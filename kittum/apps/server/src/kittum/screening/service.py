"""The screening service. Every write is a command through `kittum.runs.Commands`: a pure
decider, one ledger event or more, the module's rows, and a receipt, in the caller's transaction.
A replayed command returns the stored outcome and writes nothing."""

import dataclasses
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from psycopg.types.json import Jsonb

from kittum.core import SystemClock, content_hash, new_id, sha256_hex
from kittum.judge import ScreeningItem
from kittum.ledger import EventDraft
from kittum.protocol import ScreeningMode, get_version
from kittum.runs import (
    Command,
    Commands,
    JobSpec,
    PostgresJobQueue,
    PostgresReceipts,
)
from kittum.runs import (
    Decision as RunDecision,
)
from kittum.screening import store
from kittum.screening.metrics import record_outcomes, stage_metrics
from kittum.screening.rules import (
    SCREENERS_PER_MODE,
    ai_rank,
    conflict_kinds,
    criteria_for,
    default_seed,
    highlights,
    percent_of,
    reveal_for,
    sections,
    seeded_sample,
    sort_key,
    topic_for,
    validate,
)
from kittum.screening.types import (
    AdjudicationItem,
    AdjudicationResult,
    AiColumn,
    DecideResult,
    Decision,
    InvalidDecision,
    NotAssigned,
    QueueItem,
    Reveal,
    ReviewerColumn,
    ScopeStatus,
    StageInfo,
    StaleRevision,
    UndoResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kittum.core import Clock, Id
    from kittum.db import Tx
    from kittum.judge import Verdict
    from kittum.ledger import AuditEvent, Ledger
    from kittum.protocol import Criterion, ReviewProtocol, Stage
    from kittum.runs import JobQueue, Receipt, ReceiptStore
    from kittum.screening.metrics import RecordOutcome
    from kittum.screening.rules import Reading
    from kittum.screening.types import (
        ConflictKind,
        DecisionTiming,
        Phase,
        ScreeningRecord,
        StageSpec,
    )

STAGE_OPENED = "screening.stage_opened"
JUDGING_REQUESTED = "screening.judging_requested"
AI_READ = "screening.ai_read"
DECIDED = "screening.decided"
UNDONE = "screening.undone"
CONFLICT_OPENED = "screening.conflict_opened"
CONFLICT_UPDATED = "screening.conflict_updated"
CONFLICT_WITHDRAWN = "screening.conflict_withdrawn"
CONFLICT_PUBLISHED = "screening.conflict_published"
CONFLICT_HIDDEN = "screening.conflict_hidden"
RELEASED_WITHOUT_COVER = "screening.released_without_cover"
ADJUDICATED = "screening.adjudicated"
JUDGE_JOB = "screening.judge"


def _event(project_id: Id, type: str, actor_id: Id, payload: dict[str, Any]) -> EventDraft:
    return EventDraft(
        project_id=project_id, type=type, schema_version=1, actor_id=actor_id, payload=payload
    )


def _json(value: Any) -> Any:
    return value.model_dump(mode="json") if value is not None else None


class _Item(NamedTuple):
    record: ScreeningRecord
    kind: Literal["record", "sentinel"]
    expected: str | None
    audit: bool


AI_KINDS = frozenset({"ai_include", "ai_disagreement"})
"""Conflicts the AI alone raises. Their membership in the adjudication queue would reveal the AI's
reading, so decoys accompany them."""


def _conflict_id() -> Id:
    """A random (version 4) ID. Adjudicators see conflict IDs; time-ordered ones would tell a
    conflict from the decoy created after it."""
    return uuid.uuid4()


@dataclass(frozen=True)
class _ConflictChange:
    action: Literal["open", "update", "withdraw", "publish", "hide", "release"]
    conflict_id: Id
    item_ref: str
    kinds: tuple[ConflictKind, ...] = ()
    pattern: str | None = None  # the screeners' shared outcome, if they share one
    decoy: bool = False
    visible: bool = True  # False while a conflict only the AI raised waits for a decoy
    partner_id: Id | None = None  # the conflict a decoy covers
    assigned_to: Id | None = None  # the one adjudicator of a conflict released without cover


_EVENT_OF = {
    "open": CONFLICT_OPENED,
    "update": CONFLICT_UPDATED,
    "withdraw": CONFLICT_WITHDRAWN,
    "publish": CONFLICT_PUBLISHED,
    "hide": CONFLICT_HIDDEN,
    "release": RELEASED_WITHOUT_COVER,
}


@dataclass(frozen=True)
class _DecideChanges:
    stage_id: Id
    decision_id: Id
    state: str
    revealed: bool
    revision: int
    provisional_id: Id | None
    final_id: Id | None
    reveal: Reveal | None
    conflicts: tuple[_ConflictChange, ...]


class Screening:
    """Screening for one ledger, job queue, and receipt store.

    screening = Screening.postgres(ledger)
    stage = screening.open_stage(tx, command_id=..., project_id=..., actor_id=..., spec=spec)
    screening.request_judging(tx, command_id=..., project_id=..., actor_id=..., stage_id=...)
    # a Worker runs `judge_handler` over the jobs; then reviewers screen:
    items = screening.queue(tx, project_id, reviewer_id, scope_id, stage)
    result = screening.decide(tx, command_id=..., ..., phase="provisional", ...)
    """

    def __init__(
        self,
        ledger: Ledger,
        queue: JobQueue,
        receipts: ReceiptStore,
        clock: Clock | None = None,
        *,
        judge_batch_size: int = 25,
    ) -> None:
        self._clock = clock or SystemClock()
        self._commands = Commands(ledger, queue, receipts, self._clock)
        self._batch = judge_batch_size
        self._protocols: dict[Id, ReviewProtocol] = {}

    @classmethod
    def postgres(cls, ledger: Ledger, clock: Clock | None = None) -> Screening:
        return cls(ledger, PostgresJobQueue(clock), PostgresReceipts(), clock)

    # Stages

    def _protocol(self, tx: Tx, project_id: Id, version_id: Id) -> ReviewProtocol:
        if version_id not in self._protocols:
            self._protocols[version_id] = get_version(tx, project_id, version_id).version.protocol
        return self._protocols[version_id]

    def _criteria(self, tx: Tx, row: dict[str, Any]) -> tuple[Criterion, ...]:
        protocol = self._protocol(tx, row["project_id"], row["protocol_version_id"])
        topic = topic_for(protocol, row["scope_id"], row["topic_id"])
        return criteria_for(topic, row["scope_id"], row["stage"])

    def open_stage(
        self, tx: Tx, *, command_id: Id, project_id: Id, actor_id: Id, spec: StageSpec
    ) -> StageInfo:
        """Open screening of one scope at one stage: items, sentinels, assignments, audit sample.

        The protocol version fixes the mode (two screeners in M1 and M3, one in M2) and the audit
        plan: `sentinel_percent` of the record count is drawn from `spec.sentinels` and given to
        every reviewer, and `random_audit_percent` of the records go to `spec.auditor_id` as
        well. Both draws are seeded, so anyone can redraw them; the ledger event names them.
        """
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.open_stage",
            actor_id=actor_id,
            payload=spec.model_dump(mode="json"),
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            stored = get_version(tx, project_id, spec.protocol_version_id)
            people = [
                *spec.screeners,
                *spec.adjudicators,
                *([spec.auditor_id] if spec.auditor_id else []),
            ]
            rows = tx.execute("select id, kind from actor where id = any(%s)", (people,))
            existing = tx.execute(
                "select topic_id from screening_stage where project_id = %s and scope_id = %s"
                " and stage = %s",
                (project_id, spec.scope_id, spec.stage),
            ).fetchall()
            return {
                "stored": stored,
                "kinds": {r["id"]: r["kind"] for r in rows.fetchall()},
                "open_topics": {r["topic_id"] for r in existing},
            }

        def decide(command: Command, state: dict[str, Any]) -> RunDecision[dict[str, Any]]:
            stored = state["stored"]
            if stored.status != "published":
                raise InvalidDecision("screening needs a published protocol version")
            protocol = stored.version.protocol
            topic = topic_for(protocol, spec.scope_id, spec.topic_id)
            if topic.id in state["open_topics"]:
                raise InvalidDecision(f"{spec.scope_id} is already open at {spec.stage}")
            if not criteria_for(topic, spec.scope_id, spec.stage):
                raise InvalidDecision(f"no criterion of {topic.id} is decidable at {spec.stage}")
            mode = topic.mode
            screeners = tuple(spec.screeners)
            if len(set(screeners)) != len(screeners) or len(screeners) != SCREENERS_PER_MODE[mode]:
                raise InvalidDecision(
                    f"mode {mode} needs {SCREENERS_PER_MODE[mode]} different screeners"
                )
            people = [
                *screeners,
                *spec.adjudicators,
                *([spec.auditor_id] if spec.auditor_id else []),
            ]
            if any(state["kinds"].get(p) != "human" for p in people):
                raise InvalidDecision("every screener, auditor, and adjudicator must be human")
            if len(set(spec.adjudicators)) != len(spec.adjudicators) or set(
                spec.adjudicators
            ) & set(screeners):
                raise InvalidDecision("adjudicators must be different people from the screeners")
            refs = [r.item_ref for r in spec.records]
            sentinel_refs = [s.record.item_ref for s in spec.sentinels]
            if len(set(refs) | set(sentinel_refs)) != len(refs) + len(sentinel_refs):
                raise InvalidDecision("item references must be unique across records and sentinels")
            audit_n = percent_of(topic.audit.random_audit_percent, len(refs))
            if audit_n and spec.auditor_id is None:
                raise InvalidDecision("the audit plan samples records, so it needs an auditor")
            if not audit_n and spec.auditor_id is not None:
                raise InvalidDecision("the audit plan samples no records; leave out the auditor")
            if spec.auditor_id in screeners:
                raise InvalidDecision("the auditor must not screen the same records")
            sentinel_n = percent_of(topic.audit.sentinel_percent, len(refs))
            if sentinel_n > len(spec.sentinels):
                raise InvalidDecision(
                    f"the audit plan seeds {sentinel_n} sentinels; {len(spec.sentinels)} given"
                )
            seed = spec.seed or default_seed(
                stored.version.content_hash, topic.id, spec.scope_id, spec.stage
            )
            chosen = set(seeded_sample(seed, "sentinel", sentinel_refs, sentinel_n))
            audited = set(seeded_sample(seed, "audit", refs, audit_n))
            stage_id = new_id()
            items = [_Item(r, "record", None, r.item_ref in audited) for r in spec.records] + [
                _Item(s.record, "sentinel", s.expected_outcome, False)
                for s in spec.sentinels
                if s.record.item_ref in chosen
            ]
            shared = mode == ScreeningMode.M3
            assignments = []
            for it in items:
                ref = it.record.item_ref
                reviewers = [(s, "screener") for s in screeners]
                if spec.auditor_id and (it.audit or it.kind == "sentinel"):
                    reviewers.append((spec.auditor_id, "auditor"))
                for reviewer, role in reviewers:
                    key = sort_key(
                        seed, ref, "" if shared and role == "screener" else str(reviewer)
                    )
                    assignments.append((new_id(), stage_id, ref, reviewer, role, key))
            info = StageInfo(
                stage_id=stage_id,
                project_id=project_id,
                scope_id=spec.scope_id,
                stage=spec.stage,
                topic_id=topic.id,
                mode=mode,
                reveal=spec.reveal,
                protocol_hash=stored.version.content_hash,
                judge_config_id=spec.judge_config_id,
                screeners=screeners,
                auditor_id=spec.auditor_id,
                adjudicators=tuple(spec.adjudicators),
            )
            payload = {
                **info.model_dump(mode="json"),
                "protocol_version_id": str(spec.protocol_version_id),
                "seed": seed,
                "records": len(refs),
                "records_hash": content_hash(sorted(refs), domain="kittum.screening.items.v1"),
                "sentinel_refs": sorted(chosen),
                "audit_refs": sorted(audited),
                "assignments": len(assignments),
            }
            return RunDecision(
                events=(_event(project_id, STAGE_OPENED, command.actor_id, payload),),
                changes={"info": info, "seed": seed, "items": items, "assignments": assignments},
            )

        def apply(tx: Tx, decision: RunDecision[dict[str, Any]], events: tuple[AuditEvent, ...]):
            changes = decision.changes
            assert changes is not None
            info: StageInfo = changes["info"]
            tx.execute(
                "insert into screening_stage (id, project_id, protocol_version_id, protocol_hash,"
                " topic_id, scope_id, stage, mode, reveal, judge_config_id, seed, screeners,"
                " auditor_id, adjudicators, opened_event_id, opened_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    info.stage_id,
                    project_id,
                    spec.protocol_version_id,
                    info.protocol_hash,
                    info.topic_id,
                    info.scope_id,
                    info.stage,
                    info.mode.value,
                    info.reveal,
                    info.judge_config_id,
                    changes["seed"],
                    list(info.screeners),
                    info.auditor_id,
                    list(info.adjudicators),
                    events[0].event_id,
                    events[0].recorded_at,
                ),
            )
            with tx.cursor() as cur:
                cur.executemany(
                    "insert into screening_item (stage_id, item_ref, kind, expected_outcome,"
                    " audit, title, text, facts) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            info.stage_id,
                            it.record.item_ref,
                            it.kind,
                            it.expected,
                            it.audit,
                            it.record.title,
                            it.record.text,
                            Jsonb(it.record.facts.model_dump(mode="json")),
                        )
                        for it in changes["items"]
                    ],
                )
                cur.executemany(
                    "insert into screening_assignment (id, stage_id, item_ref, reviewer_id, role,"
                    " sort_key) values (%s, %s, %s, %s, %s, %s)",
                    changes["assignments"],
                )

        self._commands.execute(tx, command, decide, load=load, apply=apply)
        protocol = self._protocol(tx, project_id, spec.protocol_version_id)
        topic = topic_for(protocol, spec.scope_id, spec.topic_id)
        return self.find_stage(tx, project_id, spec.scope_id, spec.stage, topic.id)

    def find_stage(
        self, tx: Tx, project_id: Id, scope_id: str, stage: Stage, topic_id: str | None = None
    ) -> StageInfo:
        return store.stage_info(store.find_stage(tx, project_id, scope_id, stage, topic_id))

    # AI judging

    def request_judging(
        self, tx: Tx, *, command_id: Id, project_id: Id, actor_id: Id, stage_id: Id
    ) -> Receipt:
        """Enqueue judge jobs for every item of the stage, sentinels included, in batches.

        Stage keys name the stage, the judge config, and the batch, so asking again enqueues
        nothing new.
        """
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.request_judging",
            actor_id=actor_id,
            payload={"stage_id": str(stage_id)},
        )

        def load(tx: Tx, _: Command) -> tuple[dict[str, Any], list[str]]:
            row = store.stage_row(tx, project_id, stage_id)
            refs = tx.execute(
                "select item_ref from screening_item where stage_id = %s order by item_ref",
                (stage_id,),
            ).fetchall()
            return row, [r["item_ref"] for r in refs]

        def decide(command: Command, state: tuple[dict[str, Any], list[str]]) -> RunDecision[None]:
            row, refs = state
            batches = [refs[i : i + self._batch] for i in range(0, len(refs), self._batch)]
            jobs = tuple(
                JobSpec(
                    project_id=project_id,
                    kind=JUDGE_JOB,
                    stage_key=f"{JUDGE_JOB}:{stage_id}:{row['judge_config_id']}:{n}",
                    payload={"stage_id": str(stage_id), "item_refs": batch},
                )
                for n, batch in enumerate(batches)
            )
            payload = {
                "stage_id": str(stage_id),
                "judge_config_id": row["judge_config_id"],
                "items": len(refs),
                "jobs": [j.stage_key for j in jobs],
            }
            return RunDecision(
                events=(_event(project_id, JUDGING_REQUESTED, command.actor_id, payload),),
                jobs=jobs,
            )

        return self._commands.execute(tx, command, decide, load=load)

    def judge_items(
        self, tx: Tx, project_id: Id, stage_id: Id, item_refs: Sequence[str]
    ) -> tuple[StageInfo, list[ScreeningItem], tuple[Criterion, ...]]:
        """What a judge job sends to `kittum.judge`: the items and the scope's criteria."""
        row = store.stage_row(tx, project_id, stage_id)
        rows = tx.execute(
            "select item_ref, title, text from screening_item where stage_id = %s"
            " and item_ref = any(%s)",
            (stage_id, list(item_refs)),
        ).fetchall()
        by_ref = {r["item_ref"]: r for r in rows}
        items = [
            ScreeningItem(
                item_ref=ref,
                stage=row["stage"],
                title=by_ref[ref]["title"],
                text=by_ref[ref]["text"],
            )
            for ref in item_refs
            if ref in by_ref
        ]
        return store.stage_info(row), items, self._criteria(tx, row)

    def attach_verdicts(
        self,
        tx: Tx,
        *,
        command_id: Id,
        project_id: Id,
        actor_id: Id,
        stage_id: Id,
        verdicts: Sequence[Verdict],
    ) -> Receipt:
        """Record the judge's verdicts as the stage's AI readings. Items already read keep their
        reading."""
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.attach_verdicts",
            actor_id=actor_id,
            payload={"stage_id": str(stage_id), "items": [v.item_ref for v in verdicts]},
        )

        def load(tx: Tx, _: Command) -> tuple[dict[str, Any], set[str], set[str]]:
            row = store.stage_row(tx, project_id, stage_id)
            items = tx.execute(
                "select i.item_ref, r.item_ref is not null as read from screening_item i"
                " left join screening_ai_reading r"
                " on r.stage_id = i.stage_id and r.item_ref = i.item_ref where i.stage_id = %s",
                (stage_id,),
            ).fetchall()
            return (
                row,
                {r["item_ref"] for r in items},
                {r["item_ref"] for r in items if r["read"]},
            )

        def decide(
            command: Command, state: tuple[dict[str, Any], set[str], set[str]]
        ) -> RunDecision[list[Verdict]]:
            row, refs, read = state
            new: dict[str, Verdict] = {}
            for v in verdicts:
                if v.item_ref not in refs or v.scope_id != row["scope_id"]:
                    raise InvalidDecision(f"verdict for {v.item_ref} is not for this stage")
                if v.judge_config_id != row["judge_config_id"]:
                    raise InvalidDecision(f"the stage is judged by {row['judge_config_id']}")
                if v.item_ref not in read:
                    new.setdefault(v.item_ref, v)
            if not new:
                return RunDecision()
            payload = {
                "stage_id": str(stage_id),
                "judge_config_id": row["judge_config_id"],
                "readings": [
                    {
                        "item_ref": v.item_ref,
                        "excludes": v.excludes,
                        "rank": ai_rank(v),
                        "raw_output_hash": v.raw_output_hash,
                    }
                    for v in new.values()
                ],
            }
            return RunDecision(
                events=(_event(project_id, AI_READ, command.actor_id, payload),),
                changes=list(new.values()),
            )

        def apply(tx: Tx, decision: RunDecision[list[Verdict]], events: tuple[AuditEvent, ...]):
            with tx.cursor() as cur:
                cur.executemany(
                    "insert into screening_ai_reading (stage_id, item_ref, judge_config_id,"
                    " excludes, rank, criteria, flags, raw_output_hash, event_id, recorded_at)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            stage_id,
                            v.item_ref,
                            v.judge_config_id,
                            v.excludes,
                            ai_rank(v),
                            Jsonb([c.model_dump(mode="json") for c in v.criteria]),
                            list(v.flags),
                            v.raw_output_hash,
                            events[0].event_id,
                            events[0].recorded_at,
                        )
                        for v in decision.changes or ()
                    ],
                )

        return self._commands.execute(tx, command, decide, load=load, apply=apply)

    # The reviewer's queue

    def queue(
        self,
        tx: Tx,
        project_id: Id,
        reviewer_id: Id,
        scope_id: str,
        stage: Stage,
        *,
        after: Id | None = None,
        limit: int = 5,
        topic_id: str | None = None,
    ) -> list[QueueItem]:
        """The reviewer's next undecided assignments in queue order, after `after`.

        Reads only `screening_reviewer_queue`, which cannot return another reviewer's rows, AI
        output, or sentinel and audit flags. An item enters the queue once the AI has read it,
        so a reveal is always possible and M3 can order by the AI.
        """
        row = store.find_stage(tx, project_id, scope_id, stage, topic_id)
        criteria = self._criteria(tx, row)
        rows = tx.execute(
            "with q as (select * from screening_reviewer_queue(%s, %s))"
            " select * from q where state <> 'decided'"
            " and position > coalesce((select position from q where assignment_id = %s), 0)"
            " order by position limit %s",
            (row["id"], reviewer_id, after, limit),
        ).fetchall()
        return [
            QueueItem(
                assignment_id=r["assignment_id"],
                stage=r["stage"],
                scope_id=r["scope_id"],
                title=r["title"],
                text=r["text"],
                highlights=highlights(r["text"], criteria),
                revision=r["revision"],
                record=store.queue_record(
                    r["facts"],
                    title_highlights=highlights(r["title"], criteria),
                    sections=sections(r["text"]),
                ),
                phase="final" if r["state"] == "awaiting_final" else "provisional",
                reveal=Reveal.model_validate(r["own_reveal"]) if r["own_reveal"] else None,
            )
            for r in rows
        ]

    def decide(
        self,
        tx: Tx,
        *,
        command_id: Id,
        project_id: Id,
        reviewer_id: Id,
        assignment_id: Id,
        decision: Decision,
        phase: Phase,
        expected_revision: int,
        timing: DecisionTiming,
    ) -> DecideResult:
        """Record a provisional or final decision.

        A provisional decision is the reviewer's own, before any AI output. If the stage reveals
        and the AI disagrees (or reveals always), the result carries the reveal and the item waits
        for a final decision; otherwise the provisional decision is final at once. Once every
        screener of a record has decided, the mode's conflict rules may send it to adjudication.
        """
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.decide",
            actor_id=reviewer_id,
            payload={
                "assignment_id": str(assignment_id),
                "decision": decision.model_dump(mode="json"),
                "phase": phase,
                "expected_revision": expected_revision,
                "timing": timing.model_dump(mode="json"),
            },
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            a = self._own_assignment(tx, project_id, reviewer_id, assignment_id, lock=True)
            row = store.stage_row(tx, project_id, a["stage_id"])
            return {
                "a": a,
                "stage": row,
                "criteria": self._criteria(tx, row),
                "reading": store.reading(tx, a["stage_id"], a["item_ref"]),
                "peers": (peers := store.item_assignments(tx, a["stage_id"], a["item_ref"])),
                "conflicts": (conflicts := store.conflicts(tx, a["stage_id"], a["item_ref"])),
                "partners": _partners(tx, conflicts),
                "decoys": store.decoy_state(tx, row, a["item_ref"])
                if _may_complete(row, a, peers)
                else None,
                "held": store.held_conflicts(tx, row["id"])
                if row["mode"] in ("M1", "M2")
                and store.pending_elsewhere(tx, row["id"], a["id"]) == 0
                else None,
            }

        def decide(_: Command, st: dict[str, Any]) -> RunDecision[_DecideChanges]:
            a, row = st["a"], st["stage"]
            if a["revision"] != expected_revision:
                raise StaleRevision(
                    f"assignment {assignment_id} is at revision {a['revision']}, not "
                    f"{expected_revision}; reload the queue"
                )
            validate(decision, row["stage"], st["criteria"])
            reading: Reading | None = st["reading"]
            if reading is None:
                raise InvalidDecision("the AI has not read this item yet")
            decision_id = new_id()
            if phase == "provisional":
                if a["state"] != "pending":
                    raise InvalidDecision(
                        "this item awaits a final decision"
                        if a["state"] == "awaiting_final"
                        else "this item is decided; undo it first"
                    )
                reveal = None if a["revealed"] else reveal_for(row["reveal"], decision, reading)
                provisional_id = decision_id
                final_id = None if reveal else decision_id
                new_state = "awaiting_final" if reveal else "decided"
                provisional = decision
            else:
                if a["state"] != "awaiting_final":
                    raise InvalidDecision("a final decision follows a provisional one and a reveal")
                reveal = None
                provisional_id, final_id, new_state = (
                    a["provisional_decision_id"],
                    decision_id,
                    "decided",
                )
                provisional = store.decision_of(a, "p_")
            revision = a["revision"] + 1
            events = [
                _event(
                    project_id,
                    DECIDED,
                    reviewer_id,
                    {
                        "decision_id": str(decision_id),
                        "assignment_id": str(assignment_id),
                        "stage_id": str(row["id"]),
                        "item_ref": a["item_ref"],
                        "phase": phase,
                        "outcome": decision.outcome,
                        "reason_criterion_id": decision.reason_criterion_id,
                        "criteria": [c.model_dump(mode="json") for c in decision.criteria],
                        "evidence_anchor_ids": [str(x) for x in decision.evidence_anchor_ids],
                        "revision": revision,
                        "reveal": _json(reveal),
                        "settled": new_state == "decided",
                        "timing": timing.model_dump(mode="json"),
                    },
                )
            ]
            changes: list[_ConflictChange] = []
            if new_state == "decided" and a["kind"] == "record":
                changes = self._conflict_change(
                    row,
                    st["peers"],
                    st["conflicts"],
                    st["partners"],
                    reading,
                    a["id"],
                    provisional,
                    decision,
                )
                if st["decoys"] is not None:
                    changes = _cover(
                        st["decoys"], a, st["peers"], decision, changes, st["conflicts"]
                    )
            if new_state == "decided" and st["held"] is not None:
                changes += _release(row, st["held"], st["peers"], changes)
            events += [_conflict_event(project_id, reviewer_id, row["id"], c) for c in changes]
            return RunDecision(
                events=tuple(events),
                changes=_DecideChanges(
                    stage_id=row["id"],
                    decision_id=decision_id,
                    state=new_state,
                    revealed=a["revealed"] or reveal is not None,
                    revision=revision,
                    provisional_id=provisional_id,
                    final_id=final_id,
                    reveal=reveal,
                    conflicts=tuple(changes),
                ),
            )

        def apply(tx: Tx, result: RunDecision[_DecideChanges], events: tuple[AuditEvent, ...]):
            c = result.changes
            assert c is not None
            recorded = events[0].recorded_at
            tx.execute(
                "insert into screening_decision (id, assignment_id, command_id, event_id, phase,"
                " outcome, reason_criterion_id, criteria, evidence_anchor_ids, comment, revision,"
                " reveal, shown_at, first_key_at, decided_at, reveal_shown_at, recorded_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    c.decision_id,
                    assignment_id,
                    command_id,
                    events[0].event_id,
                    phase,
                    decision.outcome,
                    decision.reason_criterion_id,
                    Jsonb([x.model_dump(mode="json") for x in decision.criteria]),
                    list(decision.evidence_anchor_ids),
                    decision.comment,
                    c.revision,
                    Jsonb(_json(c.reveal)) if c.reveal else None,
                    timing.shown_at,
                    timing.first_key_at,
                    timing.decided_at,
                    timing.reveal_shown_at,
                    recorded,
                ),
            )
            tx.execute(
                "update screening_assignment set state = %s, revealed = %s, revision = %s,"
                " provisional_decision_id = %s, final_decision_id = %s where id = %s",
                (c.state, c.revealed, c.revision, c.provisional_id, c.final_id, assignment_id),
            )
            for change, event in zip(c.conflicts, events[1:], strict=True):
                self._write_conflict(tx, change, c.stage_id, event)

        self._commands.execute(tx, command, decide, load=load, apply=apply)
        row = tx.execute(
            "select d.*, a.reviewer_id from screening_decision d"
            " join screening_assignment a on a.id = d.assignment_id where d.command_id = %s",
            (command_id,),
        ).fetchone()
        if row is None or row["reviewer_id"] != reviewer_id:
            raise NotAssigned(f"command {command_id} recorded no decision of this reviewer")
        reveal = Reveal.model_validate(row["reveal"]) if row["reveal"] else None
        return DecideResult(
            phase="provisional" if reveal and row["phase"] == "provisional" else "final",
            reveal=reveal,
            recorded_at=row["recorded_at"],
            revision=row["revision"],
        )

    def _own_assignment(
        self, tx: Tx, project_id: Id, reviewer_id: Id, assignment_id: Id, *, lock: bool = False
    ) -> dict[str, Any]:
        a = store.assignment(tx, assignment_id, lock=lock)
        if a is None or a["reviewer_id"] != reviewer_id:
            raise NotAssigned(f"assignment {assignment_id} not found")
        store.stage_row(tx, project_id, a["stage_id"])
        return a

    def _conflict_change(
        self,
        row: dict[str, Any],
        peers: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        partners: dict[Id, dict[str, Any]],
        reading: Reading,
        own_id: Id,
        own_provisional: Decision,
        own_final: Decision,
    ) -> list[_ConflictChange]:
        finals: list[tuple[str, Decision | None]] = []
        provisionals: list[Decision] = []
        for p in peers:
            if p["id"] == own_id:
                final, provisional = own_final, own_provisional
            else:
                final = store.decision_of(p, "f_") if p["f_outcome"] else None
                provisional = store.decision_of(p, "p_") if p["p_outcome"] else None
            finals.append((p["role"], final))
            if p["role"] == "screener" and provisional is not None:
                provisionals.append(provisional)
        screener_finals = [d for role, d in finals if role == "screener"]
        if any(d is None for d in screener_finals):
            return []
        labels = {_label(d) for d in screener_finals if d is not None}
        pattern = labels.pop() if len(labels) == 1 else None
        ref = peers[0]["item_ref"]
        auditor = next((d for role, d in finals if role == "auditor"), None)
        kinds = conflict_kinds(
            ScreeningMode(row["mode"]),
            row["stage"],
            [d for d in screener_finals if d is not None],
            provisionals,
            auditor,
            reading,
        )
        open_ = next((c for c in conflicts if c["state"] == "open"), None)
        if open_ is not None and open_["decoy"]:
            # A real conflict on a decoy (an auditor's late inclusion) takes the decoy's place,
            # and the conflict it covered waits for cover again.
            if not kinds:
                return [_ConflictChange("update", open_["id"], ref, ("decoy",), pattern, True)]
            changes = [_ConflictChange("update", open_["id"], ref, kinds, pattern)]
            return changes + _uncover(partners.get(open_["partner_id"]))
        if open_ is not None:
            # Any new decision on the item bumps the revision, for decoys and conflicts alike.
            merged = tuple(dict.fromkeys((*open_["kinds"], *kinds)))
            visible = open_["visible"] or not set(merged) <= AI_KINDS
            return [
                _ConflictChange(
                    "update", open_["id"], ref, merged, open_["pattern"], False, visible
                )
            ]
        settled = {
            k for c in conflicts if c["state"] == "resolved" and not c["decoy"] for k in c["kinds"]
        }
        new = tuple(k for k in kinds if k not in settled)
        if not new:
            return []
        # A conflict only the AI raised stays invisible until a decoy covers it (`_cover`).
        return [
            _ConflictChange(
                "open", _conflict_id(), ref, new, pattern, False, not set(new) <= AI_KINDS
            )
        ]

    def _write_conflict(
        self, tx: Tx, change: _ConflictChange, stage_id: Id, event: AuditEvent
    ) -> None:
        if change.action == "open":
            tx.execute(
                "insert into screening_conflict (id, stage_id, item_ref, kinds, pattern, decoy,"
                " partner_id, visible, state, opened_event_id, opened_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s)",
                (
                    change.conflict_id,
                    stage_id,
                    change.item_ref,
                    list(change.kinds),
                    change.pattern,
                    change.decoy,
                    change.partner_id,
                    change.visible,
                    event.event_id,
                    event.recorded_at,
                ),
            )
        elif change.action == "update":
            tx.execute(
                "update screening_conflict set kinds = %s, pattern = %s, decoy = %s,"
                " visible = %s, revision = revision + 1 where id = %s",
                (
                    list(change.kinds),
                    change.pattern,
                    change.decoy,
                    change.visible,
                    change.conflict_id,
                ),
            )
        elif change.action in ("publish", "hide"):
            tx.execute(
                "update screening_conflict set visible = %s where id = %s",
                (change.action == "publish", change.conflict_id),
            )
        elif change.action == "release":
            tx.execute(
                "update screening_conflict set visible = true, released_without_cover = true,"
                " assigned_to = %s where id = %s",
                (change.assigned_to, change.conflict_id),
            )
        else:
            tx.execute(
                "update screening_conflict set state = 'withdrawn', revision = revision + 1,"
                " closed_event_id = %s where id = %s",
                (event.event_id, change.conflict_id),
            )

    def undo(
        self,
        tx: Tx,
        *,
        command_id: Id,
        project_id: Id,
        reviewer_id: Id,
        assignment_id: Id,
        expected_revision: int,
    ) -> UndoResult:
        """Take back the reviewer's decision on an item; it returns to the queue with a new
        revision. The decisions stay in the record. An open conflict on the item is withdrawn;
        an adjudicated item cannot be undone."""
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.undo",
            actor_id=reviewer_id,
            payload={"assignment_id": str(assignment_id), "expected_revision": expected_revision},
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            a = self._own_assignment(tx, project_id, reviewer_id, assignment_id, lock=True)
            conflicts = store.conflicts(tx, a["stage_id"], a["item_ref"])
            return {"a": a, "conflicts": conflicts, "partners": _partners(tx, conflicts)}

        def decide(_: Command, st: dict[str, Any]) -> RunDecision[dict[str, Any]]:
            a = st["a"]
            if a["revision"] != expected_revision:
                raise StaleRevision(
                    f"assignment {assignment_id} is at revision {a['revision']}, not "
                    f"{expected_revision}; reload the queue"
                )
            if a["state"] == "pending":
                raise InvalidDecision("there is no decision to undo")
            if any(c["state"] == "resolved" and not c["decoy"] for c in st["conflicts"]):
                raise InvalidDecision("this item was adjudicated; its decisions stand")
            undone = [
                str(x)
                for x in dict.fromkeys((a["provisional_decision_id"], a["final_decision_id"]))
                if x is not None
            ]
            revision = a["revision"] + 1
            events = [
                _event(
                    project_id,
                    UNDONE,
                    reviewer_id,
                    {
                        "assignment_id": str(assignment_id),
                        "item_ref": a["item_ref"],
                        "undone_decision_ids": undone,
                        "revision": revision,
                    },
                )
            ]
            open_ = next((c for c in st["conflicts"] if c["state"] == "open"), None)
            withdrawn: list[_ConflictChange] = []
            if open_ is not None:
                withdrawn.append(_ConflictChange("withdraw", open_["id"], a["item_ref"]))
                if open_["decoy"]:
                    withdrawn += _uncover(st["partners"].get(open_["partner_id"]))
            events += [
                _conflict_event(project_id, reviewer_id, a["stage_id"], c) for c in withdrawn
            ]
            return RunDecision(
                events=tuple(events),
                changes={
                    "undone": undone,
                    "revision": revision,
                    "withdrawn": withdrawn,
                    "stage_id": a["stage_id"],
                },
            )

        def apply(tx: Tx, result: RunDecision[dict[str, Any]], events: tuple[AuditEvent, ...]):
            c = result.changes
            assert c is not None
            tx.execute(
                "insert into screening_undo (id, assignment_id, command_id, event_id,"
                " undone_decision_ids, revision, recorded_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    assignment_id,
                    command_id,
                    events[0].event_id,
                    c["undone"],
                    c["revision"],
                    events[0].recorded_at,
                ),
            )
            tx.execute(
                "update screening_assignment set state = 'pending', revision = %s,"
                " provisional_decision_id = null, final_decision_id = null where id = %s",
                (c["revision"], assignment_id),
            )
            for change, event in zip(c["withdrawn"], events[1:], strict=True):
                self._write_conflict(tx, change, c["stage_id"], event)

        self._commands.execute(tx, command, decide, load=load, apply=apply)
        row = tx.execute(
            "select u.revision, u.recorded_at, a.reviewer_id from screening_undo u"
            " join screening_assignment a on a.id = u.assignment_id where u.command_id = %s",
            (command_id,),
        ).fetchone()
        if row is None or row["reviewer_id"] != reviewer_id:
            raise NotAssigned(f"command {command_id} undid nothing of this reviewer")
        return UndoResult(revision=row["revision"], recorded_at=row["recorded_at"])

    # Adjudication

    def adjudication_queue(
        self,
        tx: Tx,
        project_id: Id,
        adjudicator_id: Id,
        scope_id: str,
        stage: Stage,
        *,
        topic_id: str | None = None,
    ) -> list[AdjudicationItem]:
        """Open conflicts the adjudicator did not screen, with every human's final decision under a
        pseudonym. The AI column opens only after the adjudicator commits.

        Decoys are mixed in and look the same: records the humans and the AI agreed on, with the
        same visible outcome as the conflicts only the AI raised (see `_decoys`). The order is a
        hash of the conflict ID, not the time a conflict opened."""
        row = store.find_stage(tx, project_id, scope_id, stage, topic_id)
        conflicts = _adjudicable(tx, row, adjudicator_id)
        criteria = self._criteria(tx, row)
        return [
            AdjudicationItem(
                conflict_id=c["id"],
                revision=c["revision"],
                stage=row["stage"],
                scope_id=row["scope_id"],
                title=c["title"],
                text=c["text"],
                record=store.queue_record(
                    c["facts"],
                    title_highlights=highlights(c["title"], criteria),
                    sections=sections(c["text"]),
                ),
                columns=tuple(
                    col
                    for col, _ in _columns(
                        c["id"], store.item_assignments(tx, row["id"], c["item_ref"])
                    )
                ),
            )
            for c in conflicts
        ]

    def adjudicate(
        self,
        tx: Tx,
        *,
        command_id: Id,
        project_id: Id,
        adjudicator_id: Id,
        conflict_id: Id,
        expected_revision: int,
        adopted: str | None = None,
        decision: Decision | None = None,
        timing: DecisionTiming | None = None,
    ) -> AdjudicationResult:
        """Resolve a conflict by adopting a column (`adopted="A"`) or with an own decision.

        The result opens the AI column. The adjudicator must be a human who did not screen the
        record."""
        if (adopted is None) == (decision is None):
            raise InvalidDecision("adopt a column or give an own decision, not both")
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.adjudicate",
            actor_id=adjudicator_id,
            payload={
                "conflict_id": str(conflict_id),
                "expected_revision": expected_revision,
                "adopted": adopted,
                "decision": _json(decision),
            },
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            c = tx.execute(
                "select * from screening_conflict where id = %s for update", (conflict_id,)
            ).fetchone()
            if c is None or not c["visible"] or c["assigned_to"] not in (None, adjudicator_id):
                raise NotAssigned(f"conflict {conflict_id} not found")
            row = store.stage_row(tx, project_id, c["stage_id"])
            if adjudicator_id not in row["adjudicators"]:
                raise NotAssigned(f"conflict {conflict_id} not found")
            kind = tx.execute("select kind from actor where id = %s", (adjudicator_id,)).fetchone()
            return {
                "c": c,
                "stage": row,
                "criteria": self._criteria(tx, row),
                "peers": store.item_assignments(tx, c["stage_id"], c["item_ref"]),
                "human": kind is not None and kind["kind"] == "human",
            }

        def decide(_: Command, st: dict[str, Any]) -> RunDecision[Decision]:
            c, row = st["c"], st["stage"]
            if c["state"] != "open":
                raise InvalidDecision(f"conflict {conflict_id} is {c['state']}")
            if c["revision"] != expected_revision:
                raise StaleRevision(
                    f"conflict {conflict_id} is at revision {c['revision']}, "
                    f"not {expected_revision}"
                )
            if not st["human"]:
                raise InvalidDecision("an adjudicator must be a human actor")
            if any(p["reviewer_id"] == adjudicator_id for p in st["peers"]):
                raise InvalidDecision("an adjudicator must not have screened the record")
            chosen = decision
            if adopted is not None:
                columns = {col.label: col for col, _ in _columns(conflict_id, st["peers"])}
                if adopted not in columns:
                    raise InvalidDecision(f"there is no column {adopted!r}")
                col = columns[adopted]
                chosen = Decision(
                    outcome=col.outcome,
                    reason_criterion_id=col.reason_criterion_id,
                    criteria=col.criteria,
                    evidence_anchor_ids=col.evidence_anchor_ids,
                    comment=col.comment,
                )
            assert chosen is not None
            validate(chosen, row["stage"], st["criteria"])
            payload = {
                "conflict_id": str(conflict_id),
                "stage_id": str(row["id"]),
                "item_ref": c["item_ref"],
                "kinds": list(c["kinds"]),
                "adopted": adopted,
                "adopted_reviewer_id": next(
                    (
                        str(rid)
                        for col, rid in _columns(conflict_id, st["peers"])
                        if col.label == adopted
                    ),
                    None,
                ),
                "outcome": chosen.outcome,
                "reason_criterion_id": chosen.reason_criterion_id,
                "timing": _json(timing),
            }
            return RunDecision(
                events=(_event(project_id, ADJUDICATED, adjudicator_id, payload),), changes=chosen
            )

        def apply(tx: Tx, result: RunDecision[Decision], events: tuple[AuditEvent, ...]):
            chosen = result.changes
            assert chosen is not None
            tx.execute(
                "insert into screening_adjudication (id, conflict_id, adjudicator_id, command_id,"
                " event_id, adopted, outcome, reason_criterion_id, criteria, evidence_anchor_ids,"
                " comment, shown_at, decided_at, recorded_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    conflict_id,
                    adjudicator_id,
                    command_id,
                    events[0].event_id,
                    adopted,
                    chosen.outcome,
                    chosen.reason_criterion_id,
                    Jsonb([x.model_dump(mode="json") for x in chosen.criteria]),
                    list(chosen.evidence_anchor_ids),
                    chosen.comment,
                    timing.shown_at if timing else None,
                    timing.decided_at if timing else None,
                    events[0].recorded_at,
                ),
            )
            tx.execute(
                "update screening_conflict set state = 'resolved', revision = revision + 1,"
                " closed_event_id = %s where id = %s",
                (events[0].event_id, conflict_id),
            )

        self._commands.execute(tx, command, decide, load=load, apply=apply)
        row = tx.execute(
            "select j.*, c.stage_id, c.item_ref from screening_adjudication j"
            " join screening_conflict c on c.id = j.conflict_id where j.command_id = %s",
            (command_id,),
        ).fetchone()
        if row is None or row["adjudicator_id"] != adjudicator_id:
            raise NotAssigned(f"command {command_id} recorded no adjudication of this adjudicator")
        reading = store.reading(tx, row["stage_id"], row["item_ref"])
        assert reading is not None
        return AdjudicationResult(
            outcome=row["outcome"],
            reason_criterion_id=row["reason_criterion_id"],
            recorded_at=row["recorded_at"],
            ai=AiColumn(excludes=reading.excludes, criteria=reading.criteria),
        )

    # Status

    def outcomes(
        self,
        tx: Tx,
        project_id: Id,
        scope_id: str,
        stage: Stage,
        *,
        topic_id: str | None = None,
    ) -> dict[str, RecordOutcome]:
        """Each record's current outcome by `item_ref`: `include` or `exclude` (by consensus or
        adjudication), `conflict`, or None while it is not fully screened. Sentinels are not
        records; decoy adjudications change nothing. For the methods lead and later stages, never
        for reviewers."""
        return record_outcomes(tx, store.find_stage(tx, project_id, scope_id, stage, topic_id))

    def status(
        self,
        tx: Tx,
        project_id: Id,
        viewer_id: Id,
        scope_id: str,
        stage: Stage,
        *,
        topic_id: str | None = None,
    ) -> ScopeStatus:
        """Progress for the viewer. Someone who screens or audits the stage sees only their own
        counts; anyone else sees the stage, its agreement, the audit gauge, and the M3 stop test."""
        row = store.find_stage(tx, project_id, scope_id, stage, topic_id)
        viewer = tx.execute("select kind from actor where id = %s", (viewer_id,)).fetchone()
        if viewer is None or viewer["kind"] != "human":
            raise NotAssigned(f"actor {viewer_id} is not a human reviewer")
        if (
            viewer_id in row["adjudicators"]
            and viewer_id not in row["screeners"]
            and viewer_id != row["auditor_id"]
        ):
            # Pending adjudications as one count, decoys included, and nothing else of the AI.
            protocol = self._protocol(tx, project_id, row["protocol_version_id"])
            screened, remaining, _, _ = stage_metrics(
                tx, row, topic_for(protocol, scope_id, row["topic_id"])
            )
            return ScopeStatus(
                scope_id=scope_id,
                stage=stage,
                view="adjudicator",
                screened=screened,
                remaining=remaining,
                conflicts=len(_adjudicable(tx, row, viewer_id)),
                metrics=None,
            )
        if viewer_id in row["screeners"] or viewer_id == row["auditor_id"]:
            own = tx.execute(
                "select * from screening_reviewer_progress(%s, %s)", (row["id"], viewer_id)
            ).fetchone()
            assert own is not None
            return ScopeStatus(
                scope_id=scope_id,
                stage=stage,
                view="reviewer",
                screened=own["decided"],
                remaining=own["assigned"] - own["decided"],
                conflicts=None,
                metrics=None,
            )
        protocol = self._protocol(tx, project_id, row["protocol_version_id"])
        topic = topic_for(protocol, scope_id, row["topic_id"])
        screened, remaining, conflicts, metrics = stage_metrics(tx, row, topic)
        return ScopeStatus(
            scope_id=scope_id,
            stage=stage,
            view="lead",
            screened=screened,
            remaining=remaining,
            conflicts=conflicts,
            metrics=metrics,
        )


def _adjudicable(tx: Tx, row: dict[str, Any], adjudicator_id: Id) -> list[dict[str, Any]]:
    """The open conflicts an adjudicator of the stage may see: published (covered by a decoy, or
    released to them), on records they did not review, in an order hashed from random IDs."""
    if adjudicator_id not in row["adjudicators"]:
        raise NotAssigned(f"actor {adjudicator_id} does not adjudicate this stage")
    return tx.execute(
        "select c.id, c.item_ref, c.revision, i.title, i.text, i.facts"
        " from screening_conflict c"
        " join screening_item i on i.stage_id = c.stage_id and i.item_ref = c.item_ref"
        " where c.stage_id = %s and c.state = 'open' and c.visible"
        " and (c.assigned_to is null or c.assigned_to = %s)"
        " and not exists (select 1 from screening_assignment a where a.stage_id = c.stage_id"
        " and a.item_ref = c.item_ref and a.reviewer_id = %s)"
        " order by md5(c.id::text)",
        (row["id"], adjudicator_id, adjudicator_id),
    ).fetchall()


def _label(decision: Decision) -> str:
    return "exclude" if decision.outcome == "exclude" else "include"


def _may_complete(row: dict[str, Any], a: dict[str, Any], peers: list[dict[str, Any]]) -> bool:
    """Whether this screener's decision could complete a record that the AI alone can send to
    adjudication (M1 and M2), so the decoy bookkeeping is needed."""
    return (
        row["mode"] in ("M1", "M2")
        and a["kind"] == "record"
        and a["role"] == "screener"
        and all(
            p["f_outcome"] is not None
            for p in peers
            if p["role"] == "screener" and p["id"] != a["id"]
        )
    )


def _cover(
    state: dict[str, dict[str, Any]],
    a: dict[str, Any],
    peers: list[dict[str, Any]],
    decision: Decision,
    changes: list[_ConflictChange],
    conflicts: list[dict[str, Any]],
) -> list[_ConflictChange]:
    """Pair conflicts only the AI raised with decoys, and publish them together.

    Such a conflict (M1: the model includes, both humans exclude; M2: the AI disagrees with the
    lone human) shows the adjudicator only human decisions that agree, so its membership in the
    queue would reveal the AI. It opens invisible. It is published, in the same transaction, only
    with a decoy: a fully screened record with the same visible outcome and no conflict (so
    humans and AI agreed), drawn in a seeded order. If none is free when the conflict opens, the
    next record completed with that outcome becomes its decoy and both appear then. A queue item
    with a given visible outcome is therefore AI-driven with probability at most one half.
    """
    finals = [decision] + [
        store.decision_of(p, "f_") for p in peers if p["role"] == "screener" and p["id"] != a["id"]
    ]
    labels = {_label(d) for d in finals}
    if len(labels) != 1:
        return changes
    pattern = labels.pop()
    held = state["held"].get(pattern, [])
    pool = state["pool"].get(pattern, [])
    new = next((c for c in changes if c.action == "open" and not c.visible), None)
    if new is not None and pool:
        decoy = _ConflictChange(
            "open", _conflict_id(), pool[0], ("decoy",), pattern, True, partner_id=new.conflict_id
        )
        published = dataclasses.replace(new, visible=True)
        return [published if c is new else c for c in changes] + [decoy]
    if not changes and held and not any(c["state"] != "withdrawn" for c in conflicts):
        conflict_id, item_ref = held[0]
        decoy = _ConflictChange(
            "open", _conflict_id(), a["item_ref"], ("decoy",), pattern, True, partner_id=conflict_id
        )
        return [decoy, _ConflictChange("publish", conflict_id, item_ref)]
    return changes


def _uncover(partner: dict[str, Any] | None) -> list[_ConflictChange]:
    """When a decoy goes, the conflict it covered waits for cover again, unless it was released."""
    if partner is None or partner["state"] != "open" or not partner["visible"]:
        return []
    if partner["released_without_cover"]:
        return []
    return [_ConflictChange("hide", partner["id"], partner["item_ref"])]


def _release(
    row: dict[str, Any],
    held: list[dict[str, Any]],
    peers: list[dict[str, Any]],
    changes: list[_ConflictChange],
) -> list[_ConflictChange]:
    """The fallback when screening ends without cover.

    Once no assignment of the stage is left undecided, no decoy can arrive. Every conflict only
    the AI raised that still waits is then released to one adjudicator, preferably one who did
    not review the record, and flagged `released_without_cover` in the lead's view and the ledger
    (the AI disclosure reports it). The adjudicator's item looks like any other and shows no AI
    column before they commit.
    """
    published = {c.conflict_id for c in changes if c.action == "publish"}
    waiting = [
        (h["id"], h["item_ref"], set(h["reviewers"])) for h in held if h["id"] not in published
    ]
    waiting += [
        (c.conflict_id, c.item_ref, {p["reviewer_id"] for p in peers})
        for c in changes
        if c.action == "open" and not c.visible
    ]
    released = []
    for conflict_id, item_ref, reviewers in waiting:
        ranked = sorted(row["adjudicators"], key=lambda x: sort_key(row["seed"], item_ref, str(x)))
        unseen = [x for x in ranked if x not in reviewers]
        released.append(
            _ConflictChange("release", conflict_id, item_ref, assigned_to=(unseen or ranked)[0])
        )
    return released


def _partners(tx: Tx, conflicts: list[dict[str, Any]]) -> dict[Id, dict[str, Any]]:
    """The conflicts that the item's open decoys cover."""
    found = {}
    for c in conflicts:
        if c["decoy"] and c["state"] == "open" and (partner := store.conflict(tx, c["partner_id"])):
            found[partner["id"]] = partner
    return found


def _conflict_event(
    project_id: Id, actor_id: Id, stage_id: Id, change: _ConflictChange
) -> EventDraft:
    return _event(
        project_id,
        _EVENT_OF[change.action],
        actor_id,
        {
            "conflict_id": str(change.conflict_id),
            "stage_id": str(stage_id),
            "item_ref": change.item_ref,
            "kinds": list(change.kinds),
            "decoy": change.decoy,
            "visible": change.visible,
            "partner_id": str(change.partner_id) if change.partner_id else None,
            "assigned_to": str(change.assigned_to) if change.assigned_to else None,
        },
    )


def _columns(
    conflict_id: Id, assignments: Sequence[dict[str, Any]]
) -> list[tuple[ReviewerColumn, Id]]:
    """Every human's final decision on the item, in a pseudonymous order per conflict."""
    decided = sorted(
        (a for a in assignments if a["f_outcome"] is not None),
        key=lambda a: sha256_hex(f"{conflict_id}\x1f{a['reviewer_id']}".encode()),
    )
    columns = []
    for n, a in enumerate(decided):
        d = store.decision_of(a, "f_")
        columns.append(
            (
                ReviewerColumn(
                    label=chr(ord("A") + n),
                    outcome=d.outcome,
                    reason_criterion_id=d.reason_criterion_id,
                    criteria=d.criteria,
                    evidence_anchor_ids=d.evidence_anchor_ids,
                    comment=d.comment,
                ),
                a["reviewer_id"],
            )
        )
    return columns


__all__ = ["JUDGE_JOB", "Screening"]
