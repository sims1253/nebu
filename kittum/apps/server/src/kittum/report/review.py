"""The review as of a ledger cutoff: what every report target renders from.

`load_review` reads the published protocol, the searches, one frozen identity revision, the
screening stages with every human decision, and the judge configurations, keeping only what the
ledger recorded at or before `cutoff_seq`. Screening state is replayed from the append-only
decision, undo, and adjudication rows and from the ledger events of conflicts and extra screens,
not read from mutable columns, so a report rendered at an old cutoff shows the review as it stood
then.

Conflicts are human disagreements only. An AI-only disagreement (the model saw nothing wrong with
a record both humans excluded in M1, or the AI disagrees with the lone human in M2) sends the
record to a blind extra screen by the stage's auditor (`Rescreen`); if the auditor disagrees with
the screeners, that becomes a conflict like any other.

Profiles render from a `Review` too; they never query the database themselves.
"""

from collections import defaultdict
from datetime import datetime
from itertools import combinations
from typing import TYPE_CHECKING, Any, Literal, cast

from kittum.core import Id, KittumError, NotFoundError, Record
from kittum.judge import USER_TEMPLATE
from kittum.ledger import PostgresLedger
from kittum.protocol import (
    ReviewProtocol,
    ScreeningMode,
    SearchTopic,
    Source,
    Stage,
    StoredProtocol,
)
from kittum.protocol import get_version as get_protocol_version
from kittum.report.extracted import ExtractedValue, load_extracted
from kittum.report.model_calls import PromptCard, prompt_cards
from kittum.screening import (
    ADJUDICATED,
    AI_DISAGREEMENT_RESCREEN,
    CONFLICT_OPENED,
    CONFLICT_UPDATED,
    CONFLICT_WITHDRAWN,
    AuditGauge,
    AuditStratum,
    PairAgreement,
    RescreenGauge,
    RescreenReason,
    RevealSetting,
)
from kittum.search import SearchExecution, get_execution
from kittum.stats import Stratum, elusion_estimate

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from kittum.db import Tx
    from kittum.ledger import AuditEvent, Ledger
    from kittum.storage import ArtifactStore

AUDIT_CONFIDENCE = 0.95
SEARCH_EXECUTED = "search.executed"
VERDICT_RECORDED = "judge.verdict_recorded"
CORPUS_INGESTED = "corpus.ingested"

# Adapter names of the core search sources, mapped to the protocol source IDs they serve.
ASSIST_EVENT = "protocol.assist"  # `kittum.assist` writes it; the report only reads the ledger

SOURCE_ALIASES: dict[str, str] = {
    "pubmed": "medline",
    "ctgov": "clinicaltrials_gov",
    "centralexport": "central",
    "euctr": "eu_ctr",
    "eu-ctr": "eu_ctr",
}

type FinalOutcome = Literal["include", "exclude", "insufficient_information", "conflict", "pending"]


class Actor(Record):
    id: Id
    identity_ref: str
    kind: Literal["human", "model", "system"]
    organization: str | None = None


class SearchRun(Record):
    """One search execution, as recorded, with the protocol source it serves."""

    execution: SearchExecution
    event_id: Id
    event_seq: int
    actor_id: Id
    source: Source | None
    """The protocol's source for this execution, if one matches its source or platform."""

    @property
    def source_kind(self) -> str:
        return self.source.kind if self.source else "other"


class Link(Record):
    occurrence_id: Id
    report_id: Id
    study_id: Id
    relation: str
    source: str


class Identity(Record):
    """A frozen identity revision: which occurrences form which report and study."""

    revision_id: Id
    event_id: Id | None
    event_seq: int
    links: tuple[Link, ...]

    def report_of(self) -> dict[Id, Id]:
        return {link.occurrence_id: link.report_id for link in self.links}

    def study_of(self) -> dict[Id, Id]:
        return {link.report_id: link.study_id for link in self.links}

    def occurrences_of(self) -> dict[Id, tuple[Id, ...]]:
        found: dict[Id, list[Id]] = defaultdict(list)
        for link in self.links:
            found[link.report_id].append(link.occurrence_id)
        return {k: tuple(v) for k, v in found.items()}


class HumanDecision(Record):
    """A reviewer's decision that stands at the cutoff (not undone)."""

    id: Id
    assignment_id: Id
    reviewer_id: Id
    role: Literal["screener", "auditor"]
    phase: Literal["provisional", "final"]
    outcome: str
    reason_criterion_id: str | None
    revealed: bool  # the AI was shown after this provisional decision
    event_id: Id
    event_seq: int
    decided_at: datetime


class Adjudication(Record):
    id: Id
    adjudicator_id: Id
    adopted: str | None
    outcome: str
    reason_criterion_id: str | None
    event_id: Id
    event_seq: int


class Conflict(Record):
    """A conflict between humans as its ledger events left it at the cutoff."""

    id: Id
    kinds: tuple[str, ...]  # human_disagreement, exclusion_reason, audit_disagreement
    state: Literal["open", "resolved", "withdrawn"]
    opened_event_id: Id
    adjudication: Adjudication | None


class Rescreen(Record):
    """A blind extra screen by the auditor, because only the AI disagreed with the screeners."""

    reason: RescreenReason
    auditor_id: Id
    event_id: Id
    event_seq: int


class ItemResult(Record):
    """One item of a stage, and where it stands at the cutoff."""

    item_ref: str
    kind: Literal["record", "sentinel"]
    title: str
    audit: bool
    expected_outcome: str | None
    screeners: tuple[Id, ...]  # who was assigned to screen it
    reviewers: tuple[Id, ...]
    """Everyone assigned at the cutoff: the screeners, and the auditor of a sentinel, an audit
    sample, or an extra screen already triggered."""
    outcome: FinalOutcome
    """Adjudicated, in conflict, agreed by the screeners, or pending. At title and abstract
    `unclear` counts as include."""
    reason_criterion_ids: tuple[str, ...]
    provisional: tuple[HumanDecision, ...]  # the screeners' blind decisions
    final: tuple[HumanDecision, ...]  # the screeners' and auditor's standing decisions
    conflicts: tuple[Conflict, ...]
    rescreen: Rescreen | None
    ai_excludes: bool | None  # the verified AI reading; None if the AI has not read it
    ai_includes_by_own_reading: bool | None
    """The model's own reading: no criterion it answered `not_met` before the filter."""
    ai_reading_event_id: Id | None
    ai_raw_output_hash: str | None

    @property
    def screened(self) -> bool:
        """Every screener has a standing decision, and the auditor too if the record went to an
        extra screen."""
        done = {d.reviewer_id for d in self.final if d.role == "screener"}
        if not self.screeners or not set(self.screeners) <= done:
            return False
        return self.rescreen is None or self.rescreen_decision is not None

    @property
    def rescreen_decision(self) -> HumanDecision | None:
        """The auditor's standing decision on an extra screen."""
        if self.rescreen is None:
            return None
        return next((d for d in self.final if d.role == "auditor"), None)

    @property
    def rescreen_upheld(self) -> bool | None:
        """Whether the auditor agreed with the screeners; None while the extra screen is open."""
        auditor = self.rescreen_decision
        if auditor is None:
            return None
        return all(
            _label(auditor.outcome) == _label(d.outcome) for d in self.final if d.role == "screener"
        )

    @property
    def genuine(self) -> tuple[Conflict, ...]:
        """The conflicts that count: not withdrawn."""
        return tuple(c for c in self.conflicts if c.state != "withdrawn")

    @property
    def adjudication(self) -> Adjudication | None:
        return self.genuine[-1].adjudication if self.genuine else None

    @property
    def ai_revealed(self) -> bool:
        return any(d.revealed for d in self.provisional)

    @property
    def ai_rescreened(self) -> bool:
        """The AI's disagreement sent the record to an extra screen."""
        return self.rescreen is not None


class StageResult(Record):
    id: Id
    topic_id: str
    scope_id: str
    stage: Stage
    mode: ScreeningMode
    reveal: RevealSetting
    judge_config_id: str
    protocol_version_id: Id
    protocol_hash: str
    screeners: tuple[Id, ...]
    auditor_id: Id | None
    opened_event_id: Id
    items: tuple[ItemResult, ...]
    agreement: tuple[PairAgreement, ...]
    audit: AuditGauge
    rescreens: RescreenGauge
    reveals_shown: int
    revised_after_reveal: int

    @property
    def records(self) -> tuple[ItemResult, ...]:
        return tuple(i for i in self.items if i.kind == "record")

    @property
    def screened(self) -> int:
        return sum(1 for i in self.records if i.screened)

    @property
    def conflicts_open(self) -> int:
        return sum(1 for i in self.records if any(c.state == "open" for c in i.genuine))

    @property
    def conflicts_resolved(self) -> int:
        return sum(1 for i in self.records for c in i.genuine if c.state == "resolved")


class JudgeInfo(Record):
    """A judge configuration with the model recipe it pins and what the server reported."""

    id: str
    model_recipe_id: str
    recipe: dict[str, Any]
    weights_hash: str
    prompt_version: str
    prompt_hash: str
    schema_hash: str
    decoding: dict[str, Any]
    model_identities: tuple[str, ...]
    endpoints: tuple[str, ...] = ()  # where the records were sent, as recorded with each verdict
    verdicts: int


class Review(Record):
    project_id: Id
    project_name: str
    cutoff_seq: int
    protocol: StoredProtocol | None
    """The latest version published at the cutoff."""
    searches: tuple[SearchRun, ...]
    identity: Identity | None
    stages: tuple[StageResult, ...]
    judges: tuple[JudgeInfo, ...]
    actors: tuple[Actor, ...]
    protocol_ai_parts: tuple[dict[str, Any], ...] = ()
    """The fields of the protocol in force that a model wrote, as its publication recorded them."""
    assist_requests: tuple[dict[str, Any], ...] = ()
    """Every request to the protocol assistant through the cutoff (`protocol.assist` payloads)."""
    prompts: tuple[PromptCard, ...] = ()
    """What each AI role sent its model, read from the stored requests; empty without a store."""
    extracted: tuple[ExtractedValue, ...] = ()
    """The accepted extraction values at the cutoff, the latest per semantic key."""

    @property
    def document(self) -> ReviewProtocol:
        if self.protocol is None:
            raise NotFoundError(f"project {self.project_id} has no published protocol")
        return self.protocol.version.protocol

    def topic(self, topic_id: str) -> SearchTopic:
        return next(t for t in self.document.search_topics if t.id == topic_id)

    def stage(self, scope_id: str, stage: Stage, topic_id: str | None = None) -> StageResult | None:
        return next(
            (
                s
                for s in self.stages
                if s.scope_id == scope_id
                and s.stage == stage
                and (topic_id is None or s.topic_id == topic_id)
            ),
            None,
        )

    def scopes(self) -> list[tuple[str, str]]:
        """Every (topic, scope) of the protocol, in protocol order."""
        if self.protocol is None:
            return []
        return [(t.id, s.id) for t in self.document.search_topics for s in t.scopes]

    def records_by_occurrence(self) -> dict[Id, tuple[SearchRun, Any]]:
        return {r.occurrence_id: (run, r) for run in self.searches for r in run.execution.records}


class ReportRequestError(KittumError, ValueError):
    """The request cannot be answered as asked: an unknown target, the bundle asked to render, a
    profile target for a review without that profile, or a cutoff outside the ledger. The
    caller's mistake, unlike a fault of the server."""


def head_seq(tx: Tx, project_id: Id) -> int:
    row = tx.execute(
        "select coalesce(max(seq), 0) as seq from audit_event where project_id = %s", (project_id,)
    ).fetchone()
    return int(row["seq"]) if row else 0


def load_review(
    tx: Tx,
    project_id: Id,
    *,
    cutoff_seq: int | None = None,
    identity_revision_id: Id | None = None,
    ledger: Ledger | None = None,
    store: ArtifactStore | None = None,
) -> Review:
    """Read the review as the ledger recorded it through `cutoff_seq` (default: the head).

    Without `identity_revision_id`, the latest identity revision at the cutoff is used. With
    `store`, the prompts each AI role sent are read back from the stored requests.
    """
    ledger = ledger or PostgresLedger()
    project = tx.execute("select * from project where id = %s", (project_id,)).fetchone()
    if project is None:
        raise NotFoundError(f"project {project_id} not found")
    head = head_seq(tx, project_id)
    cutoff = head if cutoff_seq is None else cutoff_seq
    if not 0 <= cutoff <= head:
        raise ReportRequestError(f"cutoff {cutoff} is outside the ledger (head {head})")
    events = ledger.events(tx, project_id, through_seq=cutoff)
    seq_of = {e.event_id: e.seq for e in events}

    protocol = _protocol(tx, project_id, seq_of)
    sources = _sources(protocol.version.protocol) if protocol else {}
    searches = tuple(_searches(tx, project_id, events, sources))
    identity = _identity(tx, project_id, cutoff, identity_revision_id)
    stages = tuple(_stages(tx, project_id, events))
    judges = tuple(_judges(tx, events, stages))
    actor_ids = {e.actor_id for e in events}
    actors = tx.execute(
        "select id, identity_ref, kind, organization from actor where id = any(%s) order by id",
        (list(actor_ids),),
    ).fetchall()
    return Review(
        project_id=project_id,
        project_name=project["name"],
        cutoff_seq=cutoff,
        protocol=protocol,
        searches=searches,
        identity=identity,
        stages=stages,
        judges=judges,
        actors=tuple(Actor.model_validate(a) for a in actors),
        protocol_ai_parts=tuple(
            next(
                (
                    e.payload.get("ai_parts", [])
                    for e in events
                    if protocol and e.event_id == protocol.approved_event_id
                ),
                [],
            )
        ),
        assist_requests=tuple(e.payload for e in events if e.type == ASSIST_EVENT),
        prompts=prompt_cards(
            tx,
            project_id,
            events,
            {j.id: j.model_dump(mode="json") for j in judges},
            store,
            judge_template=USER_TEMPLATE,
        ),
        extracted=load_extracted(tx, project_id, cutoff),
    )


def _protocol(tx: Tx, project_id: Id, seq_of: Mapping[Id, int]) -> StoredProtocol | None:
    rows = tx.execute(
        "select id, approved_event_id from protocol_version"
        " where project_id = %s and status = 'published'",
        (project_id,),
    ).fetchall()
    published = [
        (seq_of[r["approved_event_id"]], r["id"]) for r in rows if r["approved_event_id"] in seq_of
    ]
    if not published:
        return None
    return get_protocol_version(tx, project_id, max(published)[1])


def _sources(protocol: ReviewProtocol) -> dict[tuple[str, str], Source]:
    """Protocol sources by (topic, source ID), and by (topic, platform) as a fallback."""
    found: dict[tuple[str, str], Source] = {}
    for topic in protocol.search_topics:
        for source in topic.source_plan.sources:
            found[topic.id, source.id] = source
            if source.platform:
                found.setdefault((topic.id, f"platform:{source.platform.casefold()}"), source)
    return found


def match_source(
    sources: Mapping[tuple[str, str], Source], topic_id: str, source: str, platform: str
) -> Source | None:
    key = source.casefold()
    return (
        sources.get((topic_id, key))
        or sources.get((topic_id, SOURCE_ALIASES.get(key, key)))
        or sources.get((topic_id, f"platform:{platform.casefold()}"))
    )


def _searches(
    tx: Tx, project_id: Id, events: Iterable[AuditEvent], sources: Mapping[tuple[str, str], Source]
) -> Iterable[SearchRun]:
    for event in events:
        if event.type != SEARCH_EXECUTED:
            continue
        execution = get_execution(tx, project_id, event.payload["execution_id"])
        if execution is None:
            raise NotFoundError(f"search execution {event.payload['execution_id']} is missing")
        plan = execution.plan
        yield SearchRun(
            execution=execution,
            event_id=event.event_id,
            event_seq=event.seq,
            actor_id=event.actor_id,
            source=match_source(sources, plan.topic_id, plan.source, plan.platform),
        )


def _identity(tx: Tx, project_id: Id, cutoff: int, revision_id: Id | None) -> Identity | None:
    row = tx.execute(
        "select id, event_id, event_seq from identity_revision"
        " where project_id = %s and finalized and event_seq <= %s"
        " and (%s::uuid is null or id = %s)"
        " order by event_seq desc, id desc limit 1",
        (project_id, cutoff, revision_id, revision_id),
    ).fetchone()
    if row is None:
        if revision_id is not None:
            raise NotFoundError(f"identity revision {revision_id} is not in the ledger by {cutoff}")
        return None
    links = tx.execute(
        "select occurrence_id, report_id, study_id, relation, source from identity_link"
        " where revision_id = %s order by occurrence_id",
        (row["id"],),
    ).fetchall()
    return Identity(
        revision_id=row["id"],
        event_id=row["event_id"],
        event_seq=row["event_seq"],
        links=tuple(Link.model_validate(link) for link in links),
    )


def _label(outcome: str | None) -> str:
    return "exclude" if outcome == "exclude" else "include"


CONFLICT_EVENTS = frozenset({CONFLICT_OPENED, CONFLICT_UPDATED, CONFLICT_WITHDRAWN, ADJUDICATED})


def conflict_states(events: Iterable[tuple[str, dict[str, Any], Id]]) -> dict[str, dict[str, Any]]:
    """Each conflict's state after the given (type, payload, event ID) events, in ledger order:
    its kinds, and whether it is open, resolved, or withdrawn."""
    states: dict[str, dict[str, Any]] = {}
    for type, payload, event_id in events:
        if type not in CONFLICT_EVENTS:
            continue
        cid = payload["conflict_id"]
        if type == CONFLICT_OPENED:
            states[cid] = {
                "kinds": tuple(payload["kinds"]),
                "state": "open",
                "opened_event_id": event_id,
                "closed_event_id": None,
            }
        elif (state := states.get(cid)) is None:
            continue
        elif type == CONFLICT_UPDATED:
            state["kinds"] = tuple(payload["kinds"])
        else:
            state["state"] = "withdrawn" if type == CONFLICT_WITHDRAWN else "resolved"
            state["closed_event_id"] = event_id
    return states


def rescreens(events: Iterable[AuditEvent]) -> dict[tuple[str, str], Rescreen]:
    """The extra screens the ledger recorded, by (stage ID, item reference)."""
    return {
        (e.payload["stage_id"], e.payload["item_ref"]): Rescreen(
            reason=e.payload["reason"],
            auditor_id=e.payload["auditor_id"],
            event_id=e.event_id,
            event_seq=e.seq,
        )
        for e in events
        if e.type == AI_DISAGREEMENT_RESCREEN
    }


def _stages(tx: Tx, project_id: Id, events: list[AuditEvent]) -> Iterable[StageResult]:
    seq_of = {e.event_id: e.seq for e in events}
    states = conflict_states((e.type, e.payload, e.event_id) for e in events)
    extra = rescreens(events)
    stages = tx.execute(
        "select * from screening_stage where project_id = %s order by opened_at, id", (project_id,)
    ).fetchall()
    for stage in stages:
        if stage["opened_event_id"] in seq_of:
            yield _stage(tx, stage, seq_of, states, extra)


def _includes_by_own_reading(criteria: list[dict[str, Any]], flags: list[str]) -> bool:
    """As screening reads it: no criterion the model answered `not_met`, before the filter; a
    record that tried to instruct the model counts as included."""
    if "injection_suspected" in flags:
        return True
    return not any((c.get("downgraded_from") or c["status"]) == "not_met" for c in criteria)


def _stage(
    tx: Tx,
    stage: dict[str, Any],
    seq_of: Mapping[Id, int],
    states: Mapping[str, dict[str, Any]],
    extra: Mapping[tuple[str, str], Rescreen],
) -> StageResult:
    stage_id = stage["id"]
    # An item withdrawn as the wrong document by the cutoff is no longer part of the stage: its
    # record has no full text again, so PRISMA counts it as not retrieved, never as excluded.
    items = [
        i
        for i in tx.execute(
            "select item_ref, kind, expected_outcome, audit, title, withdrawn_event_id"
            " from screening_item where stage_id = %s order by item_ref",
            (stage_id,),
        ).fetchall()
        if i["withdrawn_event_id"] not in seq_of
    ]
    refs = {i["item_ref"] for i in items}
    assignments = {
        a["id"]: a
        for a in tx.execute(
            "select id, item_ref, reviewer_id, role from screening_assignment where stage_id = %s",
            (stage_id,),
        ).fetchall()
        if a["item_ref"] in refs
    }
    ids = list(assignments)
    history: list[tuple[int, str, dict[str, Any]]] = []
    for d in tx.execute(
        "select * from screening_decision where assignment_id = any(%s)", (ids,)
    ).fetchall():
        if d["event_id"] in seq_of:
            history.append((seq_of[d["event_id"]], "decision", d))
    for u in tx.execute(
        "select * from screening_undo where assignment_id = any(%s)", (ids,)
    ).fetchall():
        if u["event_id"] in seq_of:
            history.append((seq_of[u["event_id"]], "undo", u))

    # Replay each assignment: a provisional decision without a reveal is final at once.
    provisional: dict[Id, HumanDecision | None] = {}
    final: dict[Id, HumanDecision | None] = {}
    for seq, kind, row in sorted(history, key=lambda h: h[0]):
        a = assignments[row["assignment_id"]]
        if kind == "undo":
            provisional[a["id"]] = final[a["id"]] = None
            continue
        decision = HumanDecision(
            id=row["id"],
            assignment_id=a["id"],
            reviewer_id=a["reviewer_id"],
            role=a["role"],
            phase=row["phase"],
            outcome=row["outcome"],
            reason_criterion_id=row["reason_criterion_id"],
            revealed=row["reveal"] is not None,
            event_id=row["event_id"],
            event_seq=seq,
            decided_at=row["decided_at"],
        )
        if row["phase"] == "provisional":
            provisional[a["id"]] = decision
            final[a["id"]] = None if decision.revealed else decision
        else:
            final[a["id"]] = decision

    readings = {
        r["item_ref"]: r
        for r in tx.execute(
            "select item_ref, excludes, criteria, flags, event_id, raw_output_hash"
            " from screening_ai_reading"
            " where stage_id = %s",
            (stage_id,),
        ).fetchall()
        if r["event_id"] in seq_of
    }
    adjudications = {
        j["conflict_id"]: j
        for j in tx.execute(
            "select j.* from screening_adjudication j join screening_conflict c"
            " on c.id = j.conflict_id where c.stage_id = %s",
            (stage_id,),
        ).fetchall()
        if j["event_id"] in seq_of
    }
    conflicts: dict[str, list[Conflict]] = defaultdict(list)
    for c in tx.execute(
        "select id, item_ref from screening_conflict where stage_id = %s", (stage_id,)
    ).fetchall():
        state = states.get(str(c["id"]))
        if state is None:
            continue
        j = adjudications.get(c["id"])
        conflicts[c["item_ref"]].append(
            Conflict(
                id=c["id"],
                kinds=state["kinds"],
                state=state["state"],
                opened_event_id=state["opened_event_id"],
                adjudication=Adjudication(
                    id=j["id"],
                    adjudicator_id=j["adjudicator_id"],
                    adopted=j["adopted"],
                    outcome=j["outcome"],
                    reason_criterion_id=j["reason_criterion_id"],
                    event_id=j["event_id"],
                    event_seq=seq_of[j["event_id"]],
                )
                if j
                else None,
            )
        )
    for found in conflicts.values():
        found.sort(key=lambda c: seq_of[c.opened_event_id])

    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in assignments.values():
        by_item[a["item_ref"]].append(a)
    results = []
    for item in items:
        ref = item["item_ref"]
        mine = by_item[ref]
        screeners = [a for a in mine if a["role"] == "screener"]
        blind: list[HumanDecision] = []
        standing: list[HumanDecision] = []
        for a in mine:
            if (blind_decision := provisional.get(a["id"])) and a["role"] == "screener":
                blind.append(blind_decision)
            if standing_decision := final.get(a["id"]):
                standing.append(standing_decision)
        screener_finals = [final.get(a["id"]) for a in screeners]
        reading = readings.get(ref)
        rescreen = extra.get((str(stage_id), ref))
        if rescreen is not None:
            # The extra screen counts once the auditor has decided it.
            auditor = next((a for a in mine if a["role"] == "auditor"), None)
            screener_finals.append(final.get(auditor["id"]) if auditor else None)
        outcome, reasons = _outcome(stage["stage"], screener_finals, conflicts.get(ref, []))
        results.append(
            ItemResult(
                item_ref=ref,
                kind=item["kind"],
                title=item["title"],
                audit=item["audit"],
                expected_outcome=item["expected_outcome"],
                screeners=tuple(a["reviewer_id"] for a in screeners),
                reviewers=tuple(
                    a["reviewer_id"]
                    for a in mine
                    if a["role"] == "screener"
                    or item["kind"] == "sentinel"
                    or item["audit"]
                    or rescreen is not None
                ),
                outcome=outcome,
                reason_criterion_ids=reasons,
                provisional=tuple(sorted(blind, key=_by_seq)),
                final=tuple(sorted(standing, key=_by_seq)),
                conflicts=tuple(conflicts.get(ref, [])),
                rescreen=rescreen,
                ai_excludes=reading["excludes"] if reading else None,
                ai_includes_by_own_reading=_includes_by_own_reading(
                    reading["criteria"], reading["flags"]
                )
                if reading
                else None,
                ai_reading_event_id=reading["event_id"] if reading else None,
                ai_raw_output_hash=reading["raw_output_hash"] if reading else None,
            )
        )
    agreement = _agreement(stage["screeners"], results)
    # Reveals and revisions over every assignment, the auditor's and sentinels' included.
    reveals = [d for d in provisional.values() if d is not None and d.revealed]
    revised = [
        d
        for d in reveals
        if (f := final.get(d.assignment_id)) is not None and f.id != d.id and f.outcome != d.outcome
    ]
    return StageResult(
        id=stage_id,
        topic_id=stage["topic_id"],
        scope_id=stage["scope_id"],
        stage=stage["stage"],
        mode=ScreeningMode(stage["mode"]),
        reveal=stage["reveal"],
        judge_config_id=stage["judge_config_id"],
        protocol_version_id=stage["protocol_version_id"],
        protocol_hash=stage["protocol_hash"],
        screeners=tuple(stage["screeners"]),
        auditor_id=stage["auditor_id"],
        opened_event_id=stage["opened_event_id"],
        items=tuple(results),
        agreement=agreement,
        audit=_audit(ScreeningMode(stage["mode"]), results),
        rescreens=_rescreens(results),
        reveals_shown=len(reveals),
        revised_after_reveal=len(revised),
    )


def _by_seq(decision: HumanDecision) -> int:
    return decision.event_seq


def _outcome(
    stage: str, finals: list[HumanDecision | None], conflicts: list[Conflict]
) -> tuple[FinalOutcome, tuple[str, ...]]:
    """Adjudicated, in conflict, agreed by every screener (and an extra screen's auditor), or
    pending."""
    settled = [c for c in conflicts if c.state != "withdrawn"]
    if settled and settled[-1].state == "open":
        return "conflict", ()
    if settled and settled[-1].adjudication is not None:
        j = settled[-1].adjudication
        return _normal(stage, j.outcome), (j.reason_criterion_id,) if j.reason_criterion_id else ()
    if not finals or any(d is None for d in finals):
        return "pending", ()
    decided = [d for d in finals if d is not None]
    outcomes = {_normal(stage, d.outcome) for d in decided}
    if outcomes == {"exclude"}:
        reasons = sorted({d.reason_criterion_id for d in decided if d.reason_criterion_id})
        return "exclude", tuple(reasons)
    if outcomes == {"insufficient_information"}:
        return "insufficient_information", ()
    return "include", ()


def _normal(stage: str, outcome: str) -> FinalOutcome:
    if outcome == "unclear" or (stage == "title_abstract" and outcome != "exclude"):
        return "include"
    return cast("FinalOutcome", outcome)


def _agreement(screeners: Iterable[Id], items: list[ItemResult]) -> tuple[PairAgreement, ...]:
    """Kappa and PABAK on the blind provisional decisions of records, as screening defines them:
    include against exclude, with unclear and insufficient information as include."""
    records = [i for i in items if i.kind == "record"]
    blind = {(d.reviewer_id, i.item_ref): d.outcome for i in records for d in i.provisional}
    people = sorted(screeners, key=str)
    pairs: list[PairAgreement] = []

    for left, right in combinations(people, 2):
        refs = [
            i.item_ref
            for i in records
            if (left, i.item_ref) in blind and (right, i.item_ref) in blind
        ]
        pairs.append(
            PairAgreement.of(
                "human_human",
                (str(left), str(right)),
                [_label(blind[left, r]) for r in refs],
                [_label(blind[right, r]) for r in refs],
            )
        )
    for person in people:
        read = [i for i in records if (person, i.item_ref) in blind and i.ai_excludes is not None]
        human = [_label(blind[person, i.item_ref]) for i in read]
        own = ["include" if i.ai_includes_by_own_reading else "exclude" for i in read]
        verified = ["exclude" if i.ai_excludes else "include" for i in read]
        pairs.append(PairAgreement.of("human_ai", (str(person), "ai"), human, own))
        pairs.append(PairAgreement.of("human_ai_verified", (str(person), "ai"), human, verified))
    return tuple(pairs)


def _audit(mode: ScreeningMode, items: list[ItemResult]) -> AuditGauge:
    """Sentinel accuracy and the elusion estimate from the random audit sample, as screening
    reports them to the methods lead."""
    records = [i for i in items if i.kind == "record"]
    sentinels = [i for i in items if i.kind == "sentinel"]
    sentinel_decisions = [(i, d) for i in sentinels for d in i.final]
    correct = [1 for i, d in sentinel_decisions if _label(d.outcome) == i.expected_outcome]

    def excluded_unseen(i: ItemResult) -> bool:
        """Excluded on the screeners' path alone, without an adjudicator looking at it."""
        path = [c for c in i.genuine if set(c.kinds) - {"audit_disagreement"}]
        finals = [d for d in i.final if d.role == "screener"]
        return i.screened and not path and all(d.outcome == "exclude" for d in finals)

    unscreened = [i for i in records if not i.screened] if mode == ScreeningMode.M3 else []
    population = [i for i in records if excluded_unseen(i)] + unscreened
    audit_final = {
        i.item_ref: d.outcome for i in records if i.audit for d in i.final if d.role == "auditor"
    }
    strata = []
    for ai_excludes in (True, False):
        members = [i for i in population if bool(i.ai_excludes) == ai_excludes]
        sampled = [i for i in members if i.item_ref in audit_final]
        strata.append(
            AuditStratum(
                ai_excludes=ai_excludes,
                population=len(members),
                sampled=len(sampled),
                relevant_found=sum(audit_final[i.item_ref] != "exclude" for i in sampled),
            )
        )
    filled = [s for s in strata if s.population]
    elusion = None
    if all(s.sampled for s in filled):
        elusion = elusion_estimate(
            [
                Stratum(population=s.population, sampled=s.sampled, relevant_found=s.relevant_found)
                for s in filled
            ],
            confidence=AUDIT_CONFIDENCE,
        )
    audited = [i for i in records if i.audit]
    return AuditGauge(
        sentinels=sum(len(i.reviewers) for i in sentinels),
        sentinels_decided=len(sentinel_decisions),
        sentinels_correct=len(correct),
        audit_samples=len(audited),
        audit_decided=sum(1 for i in audited if i.item_ref in audit_final),
        excluded_population=len(population),
        audit_relevant_found=sum(s.relevant_found for s in strata),
        strata=tuple(strata),
        elusion=elusion,
    )


def _rescreens(items: list[ItemResult]) -> RescreenGauge:
    """The extra screens AI-only disagreements triggered, as screening counts them."""
    rescreened = [i for i in items if i.kind == "record" and i.rescreen is not None]
    decided = [i for i in rescreened if i.rescreen_decision is not None]
    upheld = sum(1 for i in decided if i.rescreen_upheld)
    return RescreenGauge(
        triggered=len(rescreened),
        pending=len(rescreened) - len(decided),
        upheld=upheld,
        adjudicated=len(decided) - upheld,
    )


def _judges(
    tx: Tx, events: Iterable[AuditEvent], stages: Iterable[StageResult]
) -> Iterable[JudgeInfo]:
    verdicts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e.type == VERDICT_RECORDED:
            verdicts[e.payload["judge_config_id"]].append(e.payload)
    ids = sorted(set(verdicts) | {s.judge_config_id for s in stages})
    rows = tx.execute(
        "select * from judge_config where id = any(%s) order by id", (ids,)
    ).fetchall()
    for row in rows:
        mine = verdicts.get(row["id"], [])
        yield JudgeInfo(
            id=row["id"],
            model_recipe_id=row["model_recipe_id"],
            recipe=row["model_recipe"],
            weights_hash=row["weights_hash"],
            prompt_version=row["prompt_version"],
            prompt_hash=row["prompt_hash"],
            schema_hash=row["schema_hash"],
            decoding=row["decoding"],
            model_identities=tuple(
                sorted({v["model_identity"] for v in mine if v["model_identity"]})
            ),
            endpoints=tuple(sorted({v["endpoint"] for v in mine if v.get("endpoint")})),
            verdicts=len(mine),
        )
