"""The audit trail: what an assessor asks of the ledger, answered from its events.

- `trace`: one record's history, from the searches that found it to the values extracted from it,
  as the ledger events that touched it, each with its place in the chain;
- `events`: a page of the ledger, filtered by type, actor, or record;
- `milestones`: ledger positions with a name ("Title and abstract closed"), to pick a cutoff by;
- `as_of`: the PRISMA flows and the AI disclosure as the ledger stood at a cutoff, with the record
  behind every reference a count lists.

Everything here shows every reviewer's decisions and the AI's readings, so callers check
`require_lead` first. Nothing here writes.
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from uuid import UUID

from kittum.core import Id, NotFoundError, Record, Sha256, content_hash, sha256_hex
from kittum.ledger import PostgresLedger, event_hash, payload_hash
from kittum.report.model_calls import (
    ModelCall,
    assist_calls,
    extraction_calls,
    judge_calls,
    named_by_calls,
    transcript,
)
from kittum.report.prisma import PrismaFlow, flows
from kittum.report.renderers import disclosure
from kittum.report.review import ReportRequestError, head_seq, load_review

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kittum.db import Tx
    from kittum.ledger import AuditEvent, Ledger
    from kittum.storage import ArtifactStore

type TraceKind = Literal[
    "search",
    "identity",
    "stage",
    "ai",
    "decision",
    "conflict",
    "adjudication",
    "full_text",
    "extraction",
    "report",
    "ledger",
    "project",
]

STAGE_LABELS = {"title_abstract": "Title and abstract", "full_text": "Full text"}


class Fact(Record):
    label: str
    value: str


class ArtifactRef(Record):
    sha256: Sha256
    label: str
    stored: bool
    """Whether the artifact store holds it, so it can be downloaded."""


class TracedEvent(Record):
    """A ledger event with who recorded it, what it means, and its place in the chain."""

    seq: int
    event_id: Id
    type: str
    schema_version: int
    actor_id: Id
    actor: str
    actor_kind: str
    recorded_at: datetime
    payload_hash: Sha256
    prev_hash: Sha256
    event_hash: Sha256
    payload_hash_matches: bool
    """Whether the stored payload still hashes to `payload_hash`, recomputed for this answer."""
    event_hash_matches: bool
    """Whether `hashed_fields` still hash to `event_hash`."""
    hashed_fields: dict[str, Any]
    """What the event hash covers: the event's own fields, its payload hash, and the hash of the
    event before it."""
    kind: TraceKind
    stage: str | None
    summary: str
    facts: tuple[Fact, ...]
    artifacts: tuple[ArtifactRef, ...]
    model_calls: tuple[ModelCall, ...] = ()
    """The model requests and responses behind an AI step: a judge reading, an assistant answer,
    or the LLM extraction path."""
    payload: dict[str, Any]


class TracedOccurrence(Record):
    """One search result that the identity revision counts as this record."""

    occurrence_id: Id
    title: str
    source: str
    provider_record_id: str
    execution_id: Id
    search_seq: int | None
    raw_hash: Sha256
    """The raw response the record was parsed from."""


class RevealTiming(Record):
    """When a reviewer saw the AI's reading, against their own blind decision."""

    stage: str
    reviewer_id: Id
    reviewer: str
    decision_seq: int
    decided_at: datetime | None
    """The reviewer's own time for the blind decision, as the workbench reported it."""
    decision_recorded_at: datetime
    shown_at: datetime | None
    """When the workbench showed the reading; None until the reviewer's final decision."""
    final_seq: int | None
    kept: bool | None
    """Whether the final decision was the blind one; None while it is open."""


class RecordTrace(Record):
    ref: str
    report_id: Id | None
    """None for a search result that no identity revision holds yet: it has only its search."""
    study_id: Id | None
    title: str
    identity_revision_id: Id | None
    occurrences: tuple[TracedOccurrence, ...]
    events: tuple[TracedEvent, ...]
    reveals: tuple[RevealTiming, ...]
    head_seq: int


class EventPage(Record):
    events: tuple[TracedEvent, ...]
    head_seq: int
    next_after_seq: int | None
    """Pass as `after_seq` for the next page; None on the last."""


class Milestone(Record):
    seq: int
    kind: Literal["protocol", "search", "corpus", "stage_opened", "stage_closed", "seal", "report"]
    label: str
    recorded_at: datetime


class ItemLabel(Record):
    title: str
    trace_ref: str | None
    """The reference `trace` takes for it; None for a sentinel, which has no history."""


class StageDisclosure(Record):
    stage: str
    scope_id: str
    mode: str
    reveal: str
    records: int
    records_read_by_ai: int
    ai_proposed_exclusions: int
    reveals_shown: int
    revised_after_reveal: int
    conflicts_resolved: int
    rescreens_sent: int


class AsOf(Record):
    cutoff_seq: int
    head_seq: int
    cutoff_type: str | None
    cutoff_recorded_at: datetime | None
    flows: tuple[PrismaFlow, ...]
    stages: tuple[StageDisclosure, ...]
    disclosure: dict[str, Any]
    disclosure_hash: Sha256
    items: dict[str, ItemLabel]
    """Every reference the flows' `members` list, with its title."""


# Who and what the events name, read once per request.


class _Names:
    def __init__(self, tx: Tx, project_id: Id, store: ArtifactStore | None) -> None:
        self.store = store
        self.actors: dict[Id, tuple[str, str]] = {}
        for row in tx.execute(
            "select a.id, a.kind, a.identity_ref,"
            " (select m.display_name from project_member m where m.actor_id = a.id"
            "  and m.project_id = %s limit 1) as name"
            " from actor a where a.id in (select distinct actor_id from audit_event"
            " where project_id = %s)",
            (project_id, project_id),
        ).fetchall():
            self.actors[row["id"]] = (row["name"] or row["identity_ref"], row["kind"])
        self.stages: dict[str, tuple[str, str]] = {
            str(r["id"]): (r["stage"], r["scope_id"])
            for r in tx.execute(
                "select id, stage, scope_id from screening_stage where project_id = %s",
                (project_id,),
            ).fetchall()
        }
        self.roles: dict[str, str] = {
            str(r["id"]): r["role"]
            for r in tx.execute(
                "select a.id, a.role from screening_assignment a"
                " join screening_stage s on s.id = a.stage_id where s.project_id = %s",
                (project_id,),
            ).fetchall()
        }
        self.flags: dict[tuple[str, str], tuple[bool, bool]] = {
            (str(r["stage_id"]), r["item_ref"]): (r["audit"], r["rescreen"])
            for r in tx.execute(
                "select i.stage_id, i.item_ref, i.audit, i.rescreen from screening_item i"
                " join screening_stage s on s.id = i.stage_id"
                " where s.project_id = %s and (i.audit or i.rescreen)",
                (project_id,),
            ).fetchall()
        }
        # Each criterion's wording in the protocol's default language, from every published
        # version (the latest wins), so an event names what a criterion asks, not only its ID.
        self.criteria: dict[str, str] = {}
        for row in tx.execute(
            "select document from protocol_version where project_id = %s and status = 'published'"
            " order by published_at, id",
            (project_id,),
        ).fetchall():
            document = row["document"] or {}
            language = document.get("default_language")
            for topic in document.get("search_topics", []):
                for c in topic.get("criteria", []):
                    text = c.get("text") or {}
                    wording = text.get(language) or next(iter(text.values()), "")
                    self.criteria[c.get("id")] = wording
        self._tx = tx

    def actor(self, actor_id: Id | str | None) -> str:
        if actor_id is None:
            return "nobody"
        try:
            return self.actors.get(UUID(str(actor_id)), (str(actor_id), ""))[0]
        except ValueError:
            return str(actor_id)

    def criterion(self, criterion_id: str | None) -> str | None:
        if not criterion_id:
            return None
        wording = self.criteria.get(criterion_id)
        return f"{criterion_id}. {wording}" if wording else criterion_id

    def artifact(self, sha256: str | None, label: str) -> ArtifactRef | None:
        if not sha256:
            return None
        stored = self.store.exists(sha256) if self.store else False
        return ArtifactRef(sha256=sha256, label=label, stored=stored)

    def anchor(self, anchor_id: str) -> dict[str, Any] | None:
        return self._tx.execute(
            "select quote, boxes, start_offset, end_offset from anchor where id = %s",
            (anchor_id,),
        ).fetchone()

    def quote(self, anchor_id: str) -> str | None:
        """An anchor's quote with its pages, as a reader cites it."""
        found = self.anchor(anchor_id)
        if found is None:
            return None
        quote = found["quote"]
        exact = quote.get("exact") if isinstance(quote, dict) else quote
        pages = sorted({b.get("page") for b in found["boxes"] or [] if isinstance(b, dict)})
        where = ", ".join(f"page {p}" for p in pages if p is not None)
        return f'"{exact}"' + (f" ({where})" if where else "")

    def verdict(self, verdict_id: str | None) -> dict[str, Any] | None:
        if not verdict_id:
            return None
        return self._tx.execute(
            "select criteria, attempts from verdict where id = %s", (verdict_id,)
        ).fetchone()


STATUS_WORDS = {"met": "met", "not_met": "not met", "unclear": "unclear"}
OUTCOME_VERBS = {
    "include": "included",
    "exclude": "excluded",
    "unclear": "marked as unclear",
    "insufficient_information": "marked as insufficient information",
}
RELATIONS = {
    "same_record": "the same record",
    "same_report": "the same report",
    "same_study": "the same study",
    "report": "one report",
    "study": "one study",
}
CONFLICT_KINDS = {
    "human_disagreement": "the screeners disagree",
    "exclusion_reason": "the screeners exclude on different criteria",
    "audit_disagreement": "the auditor disagrees with the screeners",
}
RESCREEN_REASONS = {
    "ai_include": "the AI found no failing criterion in a record both screeners excluded",
    "ai_disagreement": "the AI disagrees with the one screener",
}


def _outcome(payload: dict[str, Any]) -> str:
    outcome = str(payload.get("outcome"))
    verb = OUTCOME_VERBS.get(outcome, outcome.replace("_", " "))
    reason = payload.get("reason_criterion_id")
    return f"{verb} the record" + (f" on {reason}" if reason else "")


def _seconds(start: str | None, end: str | None) -> str | None:
    if not start or not end:
        return None
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return f"{delta.total_seconds():g} s"


def _counts(statuses: dict[str, Any] | None) -> str | None:
    if not statuses:
        return None
    return ", ".join(f"{n} {status}" for status, n in sorted(statuses.items()))


def _hashed_fields(e: AuditEvent) -> dict[str, Any]:
    """The fields the event hash covers, as `kittum.ledger.event_hash` hashes them."""
    return {
        "project_id": str(e.project_id),
        "seq": e.seq,
        "event_id": str(e.event_id),
        "type": e.type,
        "schema_version": e.schema_version,
        "actor_id": str(e.actor_id),
        "recorded_at": e.recorded_at.astimezone(UTC).isoformat(),
        "payload_hash": e.payload_hash,
        "prev_hash": e.prev_hash,
    }


def _describe(
    e: AuditEvent,
    names: _Names,
    ref: str | None,
    raw_hashes: Iterable[str] = (),
) -> TracedEvent:
    """What an event means, in a sentence that names who did what, with the facts and artifacts
    worth showing and the model calls behind an AI step."""
    p = e.payload
    stage_id = p.get("stage_id")
    stage = p.get("stage") or (
        names.stages.get(str(stage_id), (None, None))[0] if stage_id else None
    )
    who = names.actor(e.actor_id)
    facts: list[tuple[str, Any]] = []
    artifacts: list[ArtifactRef | None] = []
    calls: list[ModelCall] = []
    kind: TraceKind = "project"
    summary = e.type
    t = e.type
    if t == "search.executed":
        kind = "search"
        where = p.get("platform") or p.get("source")
        summary = f"{who} searched {where} and retrieved {p.get('retrieved_count')} records"
        facts += [
            ("Source", p.get("source")),
            ("Query", p.get("strategy")),
            ("Started", p.get("started_at")),
            ("Finished", p.get("ended_at")),
            ("Records the source reported", p.get("reported_count")),
            ("Records retrieved", p.get("retrieved_count")),
        ]
        own = set(raw_hashes)
        for h in p.get("raw_artifact_hashes", []):
            label = "Raw response this record came from" if h in own else "Raw response"
            artifacts.append(names.artifact(h, label))
    elif t == "corpus.ingested":
        kind = "identity"
        summary = (
            f"{who} built the corpus from {p.get('occurrences')} search results and Kittum"
            f" proposed {p.get('proposals')} links between them"
        )
        facts.append(("Identity revision", p.get("identity_revision_id")))
    elif t in ("corpus.confirmed", "corpus.merged", "corpus.split"):
        kind = "identity"
        verb = {"corpus.confirmed": "confirmed", "corpus.merged": "merged"}.get(t, "split")
        relation = str(p.get("relation") or p.get("level") or "")
        n = len(p.get("occurrence_ids", []))
        summary = f"{who} {verb} {n} search results as {RELATIONS.get(relation, relation)}"
        facts += [
            ("Reason", p.get("reason")),
            ("Identity revision", p.get("revision_id")),
            ("Revision before", p.get("parent_revision_id")),
        ]
    elif t == "screening.stage_opened":
        kind = "stage"
        summary = (
            f"{who} opened {STAGE_LABELS.get(str(stage), stage)} screening for scope"
            f" {p.get('scope_id')}"
        )
        facts += [
            ("Mode", p.get("mode")),
            ("AI reveal", str(p.get("reveal")).replace("_", " ")),
            ("Records", p.get("records")),
        ]
        if ref is not None and ref in p.get("audit_refs", []):
            facts.append(("Audit sample", "Kittum drew this record for the audit sample"))
    elif t == "screening.judging_requested":
        kind = "ai"
        summary = f"{who} asked the AI judge to read {p.get('items')} records"
        facts.append(("Judge configuration", p.get("judge_config_id")))
    elif t == "judge.verdict_recorded":
        kind = "ai"
        statuses = p.get("statuses") or {}
        failing = [c for c, s in statuses.items() if s == "not_met"]
        if p.get("excludes"):
            summary = f"The AI judge proposed to exclude the record on {', '.join(failing)}"
        elif failing:
            summary = (
                f"The AI judge answered not met on {', '.join(failing)}, but Kittum did not find"
                " its quote in the record, so it proposes no exclusion"
            )
        else:
            summary = "The AI judge found no criterion that the record fails"
        verdict = names.verdict(p.get("verdict_id"))
        for c in (verdict or {}).get("criteria") or []:
            status = STATUS_WORDS.get(c.get("status"), c.get("status"))
            parts = [str(status).capitalize() + "."]
            if c.get("rationale"):
                parts.append(str(c["rationale"]))
            if c.get("quote"):
                parts.append(f'Quote "{c["quote"]}".')
            facts.append((names.criterion(c.get("criterion_id")) or "Criterion", " ".join(parts)))
        if not verdict:
            facts += [
                (names.criterion(c) or c, STATUS_WORDS.get(s, s)) for c, s in statuses.items()
            ]
        facts += [
            ("Model", p.get("model_identity")),
            ("Judge configuration", p.get("judge_config_id")),
            ("Flags", ", ".join(p.get("flags", [])) or None),
            ("Shown to screeners", "Only after their own decision, as the stage's reveal allows"),
        ]
        if verdict:
            calls = judge_calls(verdict["attempts"], names.store)
        artifacts.append(names.artifact(p.get("raw_output_hash"), "Answer text"))
    elif t == "screening.ai_read":
        kind = "ai"
        readings = [r for r in p.get("readings", []) if ref is None or r.get("item_ref") == ref]
        summary = (
            f"Kittum filed the AI's readings of {len(p.get('readings', []))} records with the"
            " stage. Screeners see a reading only after their own decision"
        )
        for r in readings[:1] if ref is not None else []:
            facts.append(
                (
                    "Reading of this record",
                    "Proposes to exclude" if r.get("excludes") else "No exclusion proposed",
                )
            )
            artifacts.append(names.artifact(r.get("raw_output_hash"), "Answer text"))
    elif t == "screening.decided":
        kind = "decision"
        role = names.roles.get(str(p.get("assignment_id")), "screener")
        phase = p.get("phase")
        timing = p.get("timing") or {}
        if phase == "provisional":
            summary = f"{who} {_outcome(p)}, blind"
        else:
            summary = f"{who} {_outcome(p)}"
            if timing.get("reveal_shown_at"):
                summary += ", after seeing the AI's reading"
        if role == "auditor":
            audit, rescreen = names.flags.get(
                (str(stage_id), str(p.get("item_ref"))), (False, False)
            )
            summary += (
                " (the auditor's blind extra screen)"
                if rescreen
                else " (the auditor's audit sample)"
                if audit
                else " (the auditor)"
            )
        facts += [
            ("Criterion", names.criterion(p.get("reason_criterion_id"))),
            ("Decision", "Blind, before any AI output" if phase == "provisional" else "Final"),
            ("Role", role),
            ("Comment", p.get("comment")),
        ]
        for anchor_id in p.get("evidence_anchor_ids") or []:
            facts.append(("Evidence", names.quote(str(anchor_id)) or anchor_id))
        for c in p.get("criteria") or []:
            status = c.get("status")
            if status:
                facts.append(
                    (
                        names.criterion(c.get("criterion_id")) or "Criterion",
                        STATUS_WORDS.get(status, status),
                    )
                )
        facts += [
            ("Record shown at", timing.get("shown_at")),
            ("Decided at", timing.get("decided_at")),
            ("Time to decide", _seconds(timing.get("shown_at"), timing.get("decided_at"))),
        ]
        reveal = p.get("reveal")
        if reveal:
            status = STATUS_WORDS.get(reveal.get("status"), reveal.get("status"))
            quote = f' Quote "{reveal["quote"]}".' if reveal.get("quote") else ""
            facts.append(
                (
                    "AI reading shown after this decision",
                    f"{reveal.get('criterion_id')} {status}. {reveal.get('rationale')}{quote}",
                )
            )
        if timing.get("reveal_shown_at"):
            facts.append(("AI reading shown at", timing.get("reveal_shown_at")))
    elif t == "screening.undone":
        kind = "decision"
        summary = f"{who} took back their decision"
    elif t == "screening.ai_disagreement_rescreen":
        kind = "conflict"
        reason = str(p.get("reason"))
        summary = (
            f"Kittum sent the record to {names.actor(p.get('auditor_id'))} for a blind extra"
            f" screen because {RESCREEN_REASONS.get(reason, reason.replace('_', ' '))}"
        )
    elif t.startswith("screening.conflict_"):
        kind = "conflict"
        what = {"opened": "opened", "updated": "updated", "withdrawn": "withdrew"}.get(
            t.removeprefix("screening.conflict_"), t.removeprefix("screening.conflict_")
        )
        kinds = [CONFLICT_KINDS.get(k, k.replace("_", " ")) for k in p.get("kinds", [])]
        summary = f"Kittum {what} a conflict" + (f" because {' and '.join(kinds)}" if kinds else "")
    elif t == "screening.adjudicated":
        kind = "adjudication"
        adopted = (
            f", adopting {names.actor(p.get('adopted_reviewer_id'))}'s decision"
            if p.get("adopted_reviewer_id")
            else ", with a decision of their own"
        )
        summary = f"{who} adjudicated and {_outcome(p)}{adopted}"
        facts.append(("Criterion", names.criterion(p.get("reason_criterion_id"))))
    elif t == "project.full_text_attached" and p.get("acquisition_id"):
        kind = "full_text"
        summary = f"Kittum attached the full text it fetched from {p.get('source')}"
        facts += [
            ("Licence", p.get("licence") or "Not stated"),
            ("URL", p.get("url")),
            ("Document", p.get("document_id")),
        ]
        artifacts.append(names.artifact(p.get("artifact_hash"), "Full text PDF"))
    elif t == "project.full_text_attached":
        kind = "full_text"
        summary = f"{who} uploaded the full text"
        facts += [
            ("Licence", p.get("licence") or "Not stated"),
            ("Document", p.get("document_id")),
        ]
        artifacts.append(names.artifact(p.get("artifact_hash"), "Full text PDF"))
    elif t == "document.acquired":
        kind = "full_text"
        summary = f"Kittum fetched the full text from {p.get('source')}"
        facts += [("Licence", p.get("licence") or "Not stated"), ("URL", p.get("url"))]
        artifacts.append(names.artifact(p.get("artifact_hash"), "Full text PDF"))
    elif t == "document.acquisition_failed":
        kind = "full_text"
        summary = "Kittum could not get the full text"
    elif t == "document.acquisition_attempted":
        kind = "full_text"
        attempt = p.get("attempt") or {}
        outcome = str(attempt.get("outcome") or "").replace("_", " ")
        summary = f"Kittum could not get the full text from {attempt.get('source')} ({outcome})"
        facts += [
            ("URL", attempt.get("url")),
            ("HTTP status", attempt.get("status")),
            ("Licence", attempt.get("licence")),
            ("Error", attempt.get("error")),
        ]
    elif t == "retrieval.open_access_requested":
        kind = "full_text"
        summary = (
            f"{who} asked Kittum to look for open-access full texts of"
            f" {len(p.get('report_ids', []))} reports"
        )
    elif t == "retrieval.no_open_access_location":
        kind = "full_text"
        summary = "Kittum found no open-access copy of the full text"
    elif t == "extraction.run_requested":
        kind = "extraction"
        path = (
            f"the Janus path and the LLM path with {p['model_id']}"
            if p.get("model_id")
            else "the Janus path"
        )
        summary = f"{who} asked for an extraction with {path}"
        facts += [("Schema", p.get("schema_hash")), ("Document", p.get("document_id"))]
    elif t == "extraction.proposals_recorded":
        kind = "extraction"
        summary = "Kittum recorded what both extraction paths proposed"
        statuses = p.get("statuses") or {}
        facts += [
            ("Janus path", _counts(statuses.get("janus"))),
            ("LLM path", _counts(statuses.get("llm"))),
            ("Janus version", p.get("janus_version")),
            ("Model", p.get("model_identity")),
        ]
        calls = extraction_calls(transcript(names.store, p.get("transcript_hash")), names.store)
        artifacts += [
            names.artifact(p.get("output_hash"), "Proposals"),
            names.artifact(p.get("transcript_hash"), "LLM path transcript"),
        ]
    elif t == "extraction.value_accepted":
        kind = "extraction"
        key = p.get("key") or {}
        label = " ".join(
            str(key[k]) for k in ("endpoint", "arm", "timepoint", "quantity") if key.get(k)
        ) or str(p.get("field_id"))
        unit = f" {p['unit']}" if p.get("unit") else ""
        how = "after editing it" if p.get("edited") else f"from the {p.get('path')} path"
        summary = f"{who} accepted {label} = {p.get('value')}{unit}, {how}"
        anchor = names.anchor(str(p.get("anchor_id"))) if p.get("anchor_id") else None
        if anchor:
            quote = anchor["quote"]
            pages = sorted({b.get("page") for b in anchor["boxes"] if isinstance(b, dict)})
            facts += [
                ("Quote", quote.get("exact") if isinstance(quote, dict) else quote),
                ("Page", ", ".join(str(n) for n in pages if n is not None) or None),
            ]
        facts += [
            ("Anchor", p.get("anchor_id")),
            ("Source on screen for", f"{p.get('source_viewed_ms')} ms"),
            ("Checks that failed", ", ".join(p.get("failed_checks", [])) or None),
        ]
    elif t == "report.rendered":
        kind = "report"
        summary = f"{who} rendered the {p.get('target')} report at event {p.get('cutoff_seq')}"
        artifacts.append(names.artifact(p.get("index_hash"), "List of rendered files"))
    elif t == "ledger.bundle_sealed":
        kind = "ledger"
        summary = f"{who} sealed the handover bundle at event {p.get('cutoff_seq')}"
        facts.append(("Manifest SHA-256", p.get("manifest_hash")))
    elif t == "protocol.published":
        kind = "project"
        summary = f"{who} published protocol version {p.get('version_label')}"
        facts.append(("Content hash", p.get("content_hash")))
    elif t == "protocol.assist":
        kind = "ai"
        task = (
            "draft the protocol"
            if p.get("purpose") == "draft"
            else f"improve {p.get('part') or 'a part of the protocol'}"
        )
        result = {
            "revision": "and accepted the answer as a new revision to review",
            "unchanged": "and the model suggested no change",
            "failed": "but no answer passed Kittum's checks",
        }.get(str(p.get("outcome")), "")
        summary = f"{who} asked the assistant to {task} {result}".strip()
        facts += [
            ("Model", p.get("model_identity") or p.get("recipe_id")),
            ("Endpoint", p.get("endpoint")),
            ("Prompt version", p.get("prompt_version")),
            ("Attempts", len(p.get("attempts", []))),
        ]
        calls = assist_calls(p, names.store)
    elif t == "project.member_added":
        summary = f"{who} added {p.get('display_name')} as {', '.join(p.get('roles', []))}"
    actor, actor_kind = names.actors.get(e.actor_id, (str(e.actor_id), ""))
    hashed = _hashed_fields(e)
    return TracedEvent(
        seq=e.seq,
        event_id=e.event_id,
        type=e.type,
        schema_version=e.schema_version,
        actor_id=e.actor_id,
        actor=actor,
        actor_kind=actor_kind,
        recorded_at=e.recorded_at,
        payload_hash=e.payload_hash,
        prev_hash=e.prev_hash,
        event_hash=e.event_hash,
        payload_hash_matches=payload_hash(p) == e.payload_hash,
        event_hash_matches=event_hash(
            project_id=e.project_id,
            seq=e.seq,
            event_id=e.event_id,
            type=e.type,
            schema_version=e.schema_version,
            actor_id=e.actor_id,
            recorded_at=e.recorded_at,
            payload_hash=e.payload_hash,
            prev_hash=e.prev_hash,
        )
        == e.event_hash,
        hashed_fields=hashed,
        kind=kind,
        stage=str(stage) if stage else None,
        summary=summary,
        facts=tuple(Fact(label=k, value=str(v)) for k, v in facts if k and v not in (None, "")),
        artifacts=tuple(a for a in artifacts if a is not None),
        model_calls=tuple(calls),
        payload=p,
    )


# One record's history.


class _Related:
    """Every ID through which a ledger event can concern one report, or one search result that
    no identity revision holds yet (`report_id` None, `occurrence_id` set)."""

    def __init__(
        self, tx: Tx, project_id: Id, report_id: Id | None, occurrence_id: Id | None = None
    ) -> None:
        ref = str(report_id or occurrence_id)
        self.ref = ref
        if report_id is None:
            self.occurrences = {ref}
            self.executions = {
                str(r["execution_id"])
                for r in tx.execute(
                    "select execution_id from source_occurrence where occurrence_id = %s",
                    (occurrence_id,),
                ).fetchall()
            }
            self.stages: set[str] = set()
            self.runs: set[str] = set()
            self.documents: set[str] = set()
            return
        self.occurrences = {
            str(r["occurrence_id"])
            for r in tx.execute(
                "select distinct l.occurrence_id from identity_link l"
                " join identity_revision v on v.id = l.revision_id"
                " where v.project_id = %s and l.report_id = %s",
                (project_id, report_id),
            ).fetchall()
        }
        self.executions = {
            str(r["execution_id"])
            for r in tx.execute(
                "select distinct execution_id from source_occurrence where occurrence_id = any(%s)",
                (list(self.occurrences),),
            ).fetchall()
        }
        self.stages = {
            str(r["stage_id"])
            for r in tx.execute(
                "select i.stage_id from screening_item i join screening_stage s on s.id = i.stage_id"
                " where s.project_id = %s and i.item_ref = %s",
                (project_id, ref),
            ).fetchall()
        }
        runs = tx.execute(
            "select id, document_id from extraction_run where project_id = %s and report_id = %s",
            (project_id, report_id),
        ).fetchall()
        self.runs = {str(r["id"]) for r in runs}
        self.documents = {str(r["document_id"]) for r in runs} | {
            str(r["document_id"])
            for r in tx.execute(
                "select document_id from full_text where project_id = %s and item_ref = %s",
                (project_id, ref),
            ).fetchall()
        }

    def concerns(self, e: AuditEvent) -> bool:
        p = e.payload
        ref = self.ref
        if p.get("item_ref") == ref or p.get("report_id") == ref:
            return True
        if ref in map(str, p.get("report_ids", [])):  # an open-access retrieval run's reports
            return True
        if e.type == "screening.stage_opened":
            return p.get("stage_id") in self.stages
        if e.type == "screening.ai_read":
            return any(r.get("item_ref") == ref for r in p.get("readings", []))
        if p.get("execution_id") in self.executions:
            return True
        if self.executions & set(map(str, p.get("search_ids", []))):
            return True
        if self.occurrences & set(map(str, p.get("occurrence_ids", []))):
            return True
        return p.get("document_id") in self.documents or p.get("run_id") in self.runs


class _Target(NamedTuple):
    report_id: Id | None
    study_id: Id | None
    revision_id: Id | None
    occurrence_id: Id | None  # a search result outside every identity revision


def _resolve(tx: Tx, project_id: Id, ref: str) -> _Target:
    """What a reference names: a report, a search result, or a study (its first report).

    A search result that no finalized identity revision holds (a search imported after the
    corpus was built) is still traced, by its search alone.
    """
    try:
        key = UUID(ref)
    except ValueError as e:
        raise NotFoundError(f"{ref!r} is not a record of this review") from e
    row = tx.execute(
        "select l.report_id, l.study_id, l.revision_id from identity_link l"
        " join identity_revision v on v.id = l.revision_id"
        " where v.project_id = %s and v.finalized"
        " and (l.report_id = %s or l.occurrence_id = %s or l.study_id = %s)"
        " order by v.event_seq desc, v.id desc, (l.report_id = %s) desc, l.report_id limit 1",
        (project_id, key, key, key, key),
    ).fetchone()
    if row is not None:
        return _Target(row["report_id"], row["study_id"], row["revision_id"], None)
    found = tx.execute(
        "select occurrence_id from source_occurrence where project_id = %s and occurrence_id = %s",
        (project_id, key),
    ).fetchone()
    if found is None:
        raise NotFoundError(f"{ref} is not a record of this review")
    return _Target(None, None, None, found["occurrence_id"])


def trace(
    tx: Tx,
    project_id: Id,
    ref: str,
    *,
    store: ArtifactStore | None = None,
    ledger: Ledger | None = None,
) -> RecordTrace:
    """One record's history: every ledger event that concerns it, in chain order.

    `ref` is what a PRISMA count lists: a corpus report, a search result (occurrence), or a study.
    """
    ledger = ledger or PostgresLedger()
    report_id, study_id, revision_id, occurrence_id = _resolve(tx, project_id, ref)
    related = _Related(tx, project_id, report_id, occurrence_id)
    names = _Names(tx, project_id, store)
    events = [e for e in ledger.events(tx, project_id) if related.concerns(e)]
    seq_of_search = {
        e.payload.get("execution_id"): e.seq for e in events if e.type == "search.executed"
    }
    occurrences = tuple(
        TracedOccurrence(
            occurrence_id=r["occurrence_id"],
            title=r["title"],
            source=r["source"],
            provider_record_id=r["provider_record_id"],
            execution_id=r["execution_id"],
            search_seq=seq_of_search.get(str(r["execution_id"])),
            raw_hash=r["raw_hash"],
        )
        for r in tx.execute(
            "select occurrence_id, title, source, provider_record_id, execution_id, raw_hash"
            " from source_occurrence where occurrence_id = any(%s) order by occurrence_id",
            (list(related.occurrences),),
        ).fetchall()
    )
    raw = {o.raw_hash for o in occurrences}
    title = tx.execute(
        "select title from screening_item where item_ref = %s and stage_id = any(%s) limit 1",
        (related.ref, [UUID(s) for s in related.stages]),
    ).fetchone()
    return RecordTrace(
        ref=ref,
        report_id=report_id,
        study_id=study_id,
        title=title["title"] if title else (occurrences[0].title if occurrences else ""),
        identity_revision_id=revision_id,
        occurrences=occurrences,
        events=tuple(_describe(e, names, related.ref, raw) for e in events),
        reveals=_reveals(events, names),
        head_seq=head_seq(tx, project_id),
    )


def _reveals(events: list[AuditEvent], names: _Names) -> tuple[RevealTiming, ...]:
    out: list[RevealTiming] = []
    decided = [e for e in events if e.type == "screening.decided"]
    for i, e in enumerate(decided):
        p = e.payload
        if p.get("phase") != "provisional" or not p.get("reveal"):
            continue
        final = next(
            (
                f
                for f in decided[i + 1 :]
                if f.payload.get("assignment_id") == p.get("assignment_id")
                and f.payload.get("phase") == "final"
            ),
            None,
        )
        timing = (final.payload.get("timing") if final else None) or {}
        stage = names.stages.get(str(p.get("stage_id")), ("", ""))[0]
        decided_at = (p.get("timing") or {}).get("decided_at")
        shown_at = timing.get("reveal_shown_at")
        out.append(
            RevealTiming.model_validate(
                {
                    "stage": stage,
                    "reviewer_id": e.actor_id,
                    "reviewer": names.actor(e.actor_id),
                    "decision_seq": e.seq,
                    "decided_at": decided_at,
                    "decision_recorded_at": e.recorded_at,
                    "shown_at": shown_at,
                    "final_seq": final.seq if final else None,
                    "kept": None
                    if final is None
                    else (
                        final.payload.get("outcome") == p.get("outcome")
                        and final.payload.get("reason_criterion_id") == p.get("reason_criterion_id")
                    ),
                }
            )
        )
    return tuple(out)


# The ledger, a page at a time.


def events(
    tx: Tx,
    project_id: Id,
    *,
    after_seq: int = 0,
    through_seq: int | None = None,
    types: Iterable[str] = (),
    actor_id: Id | None = None,
    ref: str | None = None,
    limit: int = 100,
    store: ArtifactStore | None = None,
    ledger: Ledger | None = None,
) -> EventPage:
    """Events after `after_seq`, oldest first. A type ending in "." matches its whole family
    ("screening."); `ref` keeps the events that concern one record, as `trace` finds them."""
    ledger = ledger or PostgresLedger()
    if not 1 <= limit <= 500:
        raise ReportRequestError("limit must be between 1 and 500")
    wanted = tuple(types)
    target = _resolve(tx, project_id, ref) if ref else None
    related = _Related(tx, project_id, target.report_id, target.occurrence_id) if target else None
    names = _Names(tx, project_id, store)

    def keep(e: AuditEvent) -> bool:
        if wanted and not any(
            e.type == w or (w.endswith(".") and e.type.startswith(w)) for w in wanted
        ):
            return False
        if actor_id is not None and e.actor_id != actor_id:
            return False
        return related is None or related.concerns(e)

    page: list[AuditEvent] = []
    more = False
    for e in ledger.events(tx, project_id, after_seq=after_seq, through_seq=through_seq):
        if not keep(e):
            continue
        if len(page) == limit:
            more = True
            break
        page.append(e)
    return EventPage(
        events=tuple(_describe(e, names, related.ref if related else None) for e in page),
        head_seq=head_seq(tx, project_id),
        next_after_seq=page[-1].seq if more and page else None,
    )


class ArtifactCheck(Record):
    """An artifact read back from the store and hashed again."""

    sha256: Sha256
    stored: bool
    bytes: int | None
    recomputed: Sha256 | None
    matches: bool
    media_type: str | None


def check_artifact(
    tx: Tx, project_id: Id, sha256: str, store: ArtifactStore | None
) -> ArtifactCheck:
    """Read an artifact the project names and hash its bytes again, to show that what the
    ledger points at is what it pointed at when it was recorded."""
    if not referenced(tx, project_id, sha256, store):
        raise NotFoundError(f"no event of the project names {sha256}")
    if store is None or not store.exists(sha256):
        return ArtifactCheck(
            sha256=sha256, stored=False, bytes=None, recomputed=None, matches=False, media_type=None
        )
    data = store.get(sha256)
    recomputed = sha256_hex(data)
    return ArtifactCheck(
        sha256=sha256,
        stored=True,
        bytes=len(data),
        recomputed=recomputed,
        matches=recomputed == sha256,
        media_type=media_type(data),
    )


def media_type(data: bytes) -> str:
    head = data[:64].lstrip()
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith((b"{", b"[")):
        return "application/json"
    if head.startswith(b"<"):
        return "application/xml"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain; charset=utf-8"


def referenced(tx: Tx, project_id: Id, sha256: str, store: ArtifactStore | None = None) -> bool:
    """Whether the project names the artifact: a ledger event, a search result's raw response,
    or a model call behind an AI step. Only those may be fetched."""
    row = tx.execute(
        "select 1 from audit_event where project_id = %s and strpos(payload::text, %s) > 0 limit 1",
        (project_id, f'"{sha256}"'),
    ).fetchone()
    if row is not None:
        return True
    row = tx.execute(
        "select 1 from source_occurrence where project_id = %s and raw_hash = %s limit 1",
        (project_id, sha256),
    ).fetchone()
    return row is not None or named_by_calls(tx, project_id, sha256, store)


# Cutoffs by name, and the review as of one.


def milestones(tx: Tx, project_id: Id, *, ledger: Ledger | None = None) -> tuple[Milestone, ...]:
    """Named ledger positions, oldest first: protocol versions, searches, the corpus, every stage
    opened, and every stage closed. A stage counts as closed at its last human event (a
    decision, an undo, an adjudication) once the next stage of its scope has opened; before
    that its last human event so far is listed as such."""
    ledger = ledger or PostgresLedger()
    names = _Names(tx, project_id, None)
    found: list[Milestone] = []
    last_human: dict[str, AuditEvent] = {}
    opened: dict[str, AuditEvent] = {}
    for e in ledger.events(tx, project_id):
        p = e.payload

        def add(kind: Any, label: str, at: AuditEvent = e) -> None:
            found.append(Milestone(seq=at.seq, kind=kind, label=label, recorded_at=at.recorded_at))

        match e.type:
            case "protocol.published":
                add("protocol", f"Protocol {p.get('version_label')} published")
            case "search.executed":
                add("search", f"Search of {p.get('platform') or p.get('source')}")
            case "corpus.ingested":
                add("corpus", "Corpus built from the searches")
            case "screening.stage_opened":
                opened[str(p.get("stage_id"))] = e
                add(
                    "stage_opened",
                    f"{STAGE_LABELS.get(str(p.get('stage')), p.get('stage'))} opened,"
                    f" {p.get('scope_id')}",
                )
            case "screening.decided" | "screening.undone" | "screening.adjudicated":
                stage_id = str(p.get("stage_id") or "")
                if not stage_id:
                    row = tx.execute(
                        "select stage_id from screening_assignment where id = %s",
                        (p.get("assignment_id"),),
                    ).fetchone()
                    stage_id = str(row["stage_id"]) if row else ""
                if stage_id:
                    last_human[stage_id] = e
            case "ledger.bundle_sealed":
                add("seal", f"Handover bundle sealed at event {p.get('cutoff_seq')}")
            case "report.rendered":
                add("report", f"{p.get('target')} report rendered")
            case _:
                pass
    for stage_id, last in last_human.items():
        own = opened.get(stage_id)
        if own is None:
            # Its opening event is gone, as a superuser deleting that row would leave it: name no
            # milestone rather than fail the screen; verify names the gap.
            continue
        stage, scope = names.stages.get(stage_id, ("", ""))
        label = STAGE_LABELS.get(stage, stage)
        later = [
            o
            for sid, o in opened.items()
            if names.stages.get(sid, ("", ""))[1] == scope and sid != stage_id and o.seq > own.seq
        ]
        closed = f"{label} closed, {scope}" if later else f"{label}, last decision so far, {scope}"
        found.append(
            Milestone(seq=last.seq, kind="stage_closed", label=closed, recorded_at=last.recorded_at)
        )
    return tuple(sorted(found, key=lambda m: (m.seq, m.kind)))


def as_of(
    tx: Tx,
    project_id: Id,
    cutoff_seq: int | None,
    *,
    ledger: Ledger | None = None,
    store: ArtifactStore | None = None,
) -> AsOf:
    """The PRISMA flows and the AI disclosure as the ledger stood at `cutoff_seq` (default: the
    head), computed as `render` would, but stored nowhere."""
    ledger = ledger or PostgresLedger()
    review = load_review(tx, project_id, cutoff_seq=cutoff_seq, ledger=ledger, store=store)
    found = flows(review)
    # Before the first protocol is published there is nothing to disclose.
    record = disclosure(review) if review.protocol is not None else {}
    titles: dict[str, ItemLabel] = {}
    for s in review.stages:
        for i in s.items:
            titles[i.item_ref] = ItemLabel(
                title=i.title, trace_ref=i.item_ref if i.kind == "record" else None
            )
    for run in review.searches:
        for r in run.execution.records:
            titles.setdefault(
                str(r.occurrence_id), ItemLabel(title=r.title, trace_ref=str(r.occurrence_id))
            )
    if review.identity:
        # Reports before (or outside) any stage: titled by a search result they hold.
        by_occurrence = review.records_by_occurrence()
        for link in review.identity.links:
            hit = by_occurrence.get(link.occurrence_id)
            if hit is not None:
                titles.setdefault(
                    str(link.report_id),
                    ItemLabel(title=hit[1].title, trace_ref=str(link.report_id)),
                )
        reports = defaultdict(list)
        for link in review.identity.links:
            reports[str(link.study_id)].append(str(link.report_id))
        for study, refs in reports.items():
            first = next((titles[r] for r in sorted(set(refs)) if r in titles), None)
            if first is not None:
                titles.setdefault(study, ItemLabel(title=first.title, trace_ref=study))
    listed = {ref for f in found for refs in f.members.values() for ref in refs}
    event = (
        ledger.events(
            tx, project_id, after_seq=review.cutoff_seq - 1, through_seq=review.cutoff_seq
        )
        if review.cutoff_seq
        else []
    )
    return AsOf(
        cutoff_seq=review.cutoff_seq,
        head_seq=head_seq(tx, project_id),
        cutoff_type=event[0].type if event else None,
        cutoff_recorded_at=event[0].recorded_at if event else None,
        flows=tuple(found),
        stages=tuple(
            StageDisclosure(
                stage=s["stage"],
                scope_id=s["scope_id"],
                mode=s["mode"],
                reveal=s["reveal"],
                records=s["records"],
                records_read_by_ai=s["records_read_by_ai"],
                ai_proposed_exclusions=s["ai_proposed_exclusions"],
                reveals_shown=s["reveals_shown"],
                revised_after_reveal=s["revised_after_reveal"],
                conflicts_resolved=s["conflicts_resolved"],
                rescreens_sent=s["rescreens"]["triggered"],
            )
            for s in record.get("stages", [])
        ),
        disclosure=record,
        disclosure_hash=content_hash(record, domain="kittum.disclosure.v1"),
        items={
            ref: titles.get(ref, ItemLabel(title=ref, trace_ref=None)) for ref in sorted(listed)
        },
    )
