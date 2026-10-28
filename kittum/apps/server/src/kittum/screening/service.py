"""The screening service. Every write is a command through `kittum.runs.Commands`: a pure
decider, one ledger event or more, the module's rows, and a receipt, in the caller's transaction.
A replayed command returns the stored outcome and writes nothing."""

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
    locate,
    locate_quote,
    percent_of,
    rescreen_reason,
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
    WithdrawResult,
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
        RescreenReason,
        ScreeningRecord,
        StageSpec,
        WithdrawReason,
    )

STAGE_OPENED = "screening.stage_opened"
JUDGING_REQUESTED = "screening.judging_requested"
AI_READ = "screening.ai_read"
DECIDED = "screening.decided"
UNDONE = "screening.undone"
ITEM_WITHDRAWN = "screening.item_withdrawn"
CONFLICT_OPENED = "screening.conflict_opened"
CONFLICT_UPDATED = "screening.conflict_updated"
CONFLICT_WITHDRAWN = "screening.conflict_withdrawn"
AI_DISAGREEMENT_RESCREEN = "screening.ai_disagreement_rescreen"
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


def _random_id() -> Id:
    """A random (version 4) ID, for rows whose IDs adjudicators see: time-ordered ones would say
    which came first."""
    return uuid.uuid4()


def _etag() -> str:
    """An opaque concurrency token: random per state, so it reveals no count of decisions."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class _ConflictChange:
    action: Literal["open", "update", "withdraw"]
    conflict_id: Id
    item_ref: str
    kinds: tuple[ConflictKind, ...] = ()
    etag: str = ""


_EVENT_OF = {
    "open": CONFLICT_OPENED,
    "update": CONFLICT_UPDATED,
    "withdraw": CONFLICT_WITHDRAWN,
}


@dataclass(frozen=True)
class _Rescreen:
    """A blind extra screen by the auditor, for an AI-only disagreement."""

    item_ref: str
    reason: RescreenReason
    assignment_id: Id | None  # a new auditor assignment, or None if the auditor has one already
    sort_key: str


@dataclass(frozen=True)
class _Withdrawal:
    """A record taken out of every full-text stage that holds it, with each stage's
    assignments."""

    item_ref: str
    stages: tuple[tuple[Id, tuple[Id, ...]], ...]


@dataclass(frozen=True)
class _DecideChanges:
    stage_id: Id
    decision_id: Id
    state: str
    revealed: bool
    revision: int
    etag: str
    provisional_id: Id | None
    final_id: Id | None
    reveal: Reveal | None
    conflicts: tuple[_ConflictChange, ...]
    rescreen: _Rescreen | None


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
        self._receipts = receipts
        self._commands = Commands(ledger, queue, receipts, self._clock)
        self._batch = judge_batch_size
        self._protocols: dict[Id, ReviewProtocol] = {}

    @classmethod
    def postgres(cls, ledger: Ledger, clock: Clock | None = None) -> Screening:
        return cls(ledger, PostgresJobQueue(clock), PostgresReceipts(), clock)

    def lock_project(self, tx: Tx, project_id: Id) -> None:
        """Hold off the project's screening commands until `tx` commits: the lock every command
        takes, for an entry point that reads before it runs one and writes after it."""
        self._receipts.lock_project(tx, project_id)

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

        The auditor's queue is also where AI-only disagreements get their blind extra screen,
        among the audit samples. So in M1 and M2 the audit plan must sample records: without
        audit samples to hide among, an extra screen would tell the auditor the AI disagreed.
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
            people = [*spec.screeners, *spec.adjudicators, spec.auditor_id]
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
            people = [*screeners, *spec.adjudicators, spec.auditor_id]
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
            if not audit_n and mode in (ScreeningMode.M1, ScreeningMode.M2):
                raise InvalidDecision(
                    f"mode {mode} sends AI-only disagreements to blind extra screens among the "
                    "audit samples, so the audit plan must sample records"
                )
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
                if it.audit or it.kind == "sentinel":
                    reviewers.append((spec.auditor_id, "auditor"))
                for reviewer, role in reviewers:
                    key = sort_key(
                        seed, ref, "" if shared and role == "screener" else str(reviewer)
                    )
                    assignments.append((_random_id(), stage_id, ref, reviewer, role, key, _etag()))
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
                    " sort_key, etag) values (%s, %s, %s, %s, %s, %s, %s)",
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

    def stage(self, tx: Tx, project_id: Id, stage_id: Id) -> StageInfo:
        return store.stage_info(store.stage_row(tx, project_id, stage_id))

    def stages(self, tx: Tx, project_id: Id) -> list[StageInfo]:
        """Every open stage of the project, title and abstract first, then by scope."""
        rows = tx.execute(
            "select * from screening_stage where project_id = %s"
            " order by stage = 'full_text', scope_id, topic_id",
            (project_id,),
        ).fetchall()
        return [store.stage_info(r) for r in rows]

    def assignment_source(
        self, tx: Tx, project_id: Id, reviewer_id: Id, assignment_id: Id
    ) -> tuple[str, StageInfo, tuple[Criterion, ...]]:
        """The record behind one of the reviewer's own assignments, its stage, and the stage's
        criteria, for an entry point that shows the record's source (the full text). Never send
        the reference to the reviewer: a sentinel's reference names it as one."""
        a = self._own_assignment(tx, project_id, reviewer_id, assignment_id)
        row = store.stage_row(tx, project_id, a["stage_id"])
        return a["item_ref"], store.stage_info(row), self._criteria(tx, row)

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
        so a reveal is always possible and M3 can order by the AI. The queue can grow behind
        `after` (an auditor's extra screen takes its record's place), so a client that pages
        forward must re-read from the start when `status.remaining` says it missed items.
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
                etag=r["etag"],
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
        expected_etag: str,
        timing: DecisionTiming,
    ) -> DecideResult:
        """Record a provisional or final decision.

        A provisional decision is the reviewer's own, before any AI output. If the stage reveals
        and the AI disagrees (or reveals always), the result carries the reveal and the item waits
        for a final decision; otherwise the provisional decision is final at once.

        Once every screener of a record has decided, humans who disagree go to adjudication. An
        AI-only disagreement (`rules.rescreen_reason`) instead gives the auditor a blind extra
        screen of the record, among their audit samples, and is recorded as
        `screening.ai_disagreement_rescreen`. If the auditor's screen then disagrees with the
        screeners, that human disagreement goes to adjudication; if it agrees, the decision
        stands. Adjudication thus never holds a record only because of the AI.
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
                "expected_etag": expected_etag,
                "timing": timing.model_dump(mode="json"),
            },
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            a = self._own_assignment(tx, project_id, reviewer_id, assignment_id, lock=True)
            row = store.stage_row(tx, project_id, a["stage_id"])
            return {
                "a": a,
                "stage": row,
                "item": store.item(tx, a["stage_id"], a["item_ref"]),
                "criteria": self._criteria(tx, row),
                "reading": store.reading(tx, a["stage_id"], a["item_ref"]),
                "peers": store.item_assignments(tx, a["stage_id"], a["item_ref"]),
                "conflicts": store.conflicts(tx, a["stage_id"], a["item_ref"]),
            }

        def decide(_: Command, st: dict[str, Any]) -> RunDecision[_DecideChanges]:
            a, row = st["a"], st["stage"]
            if a["etag"] != expected_etag:
                raise StaleRevision("This item changed since you loaded it. Reload the queue.")
            _refuse_withdrawn(a)
            validate(decision, row["stage"], st["criteria"])
            reading: Reading | None = st["reading"]
            if reading is None:
                raise InvalidDecision("The AI judge has not read this item yet.")
            decision_id = new_id()
            if phase == "provisional":
                if a["state"] != "pending":
                    raise InvalidDecision(
                        "This item waits for your final decision."
                        if a["state"] == "awaiting_final"
                        else "You decided this item already. Undo the decision first."
                    )
                reveal = None if a["revealed"] else reveal_for(row["reveal"], decision, reading)
                if reveal is not None:
                    reveal = locate(reveal, st["item"]["title"], st["item"]["text"])
                provisional_id = decision_id
                final_id = None if reveal else decision_id
                new_state = "awaiting_final" if reveal else "decided"
                provisional = decision
            else:
                if a["state"] != "awaiting_final":
                    raise InvalidDecision(
                        "A final decision comes only after a provisional one and the AI's reading."
                    )
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
            rescreen = None
            if new_state == "decided" and a["kind"] == "record":
                changes, rescreen = self._after_decision(
                    row, a, st["peers"], st["conflicts"], reading, provisional, decision
                )
            if rescreen is not None:
                payload = {
                    "stage_id": str(row["id"]),
                    "item_ref": rescreen.item_ref,
                    "reason": rescreen.reason,
                    "auditor_id": str(row["auditor_id"]),
                    "new_assignment": rescreen.assignment_id is not None,
                }
                events.append(_event(project_id, AI_DISAGREEMENT_RESCREEN, reviewer_id, payload))
            events += [_conflict_event(project_id, reviewer_id, row["id"], c) for c in changes]
            return RunDecision(
                events=tuple(events),
                changes=_DecideChanges(
                    stage_id=row["id"],
                    decision_id=decision_id,
                    state=new_state,
                    revealed=a["revealed"] or reveal is not None,
                    revision=revision,
                    etag=_etag(),
                    provisional_id=provisional_id,
                    final_id=final_id,
                    reveal=reveal,
                    conflicts=tuple(changes),
                    rescreen=rescreen,
                ),
            )

        def apply(tx: Tx, result: RunDecision[_DecideChanges], events: tuple[AuditEvent, ...]):
            c = result.changes
            assert c is not None
            recorded = events[0].recorded_at
            tx.execute(
                "insert into screening_decision (id, assignment_id, command_id, event_id, phase,"
                " outcome, reason_criterion_id, criteria, evidence_anchor_ids, comment, revision,"
                " etag, reveal, shown_at, first_key_at, decided_at, reveal_shown_at, recorded_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                    c.etag,
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
                " etag = %s, provisional_decision_id = %s, final_decision_id = %s where id = %s",
                (
                    c.state,
                    c.revealed,
                    c.revision,
                    c.etag,
                    c.provisional_id,
                    c.final_id,
                    assignment_id,
                ),
            )
            rest = list(events[1:])
            if c.rescreen is not None:
                rest.pop(0)
                tx.execute(
                    "update screening_item set rescreen = true where stage_id = %s and item_ref = %s",
                    (c.stage_id, c.rescreen.item_ref),
                )
                if c.rescreen.assignment_id is not None:
                    tx.execute(
                        "insert into screening_assignment (id, stage_id, item_ref, reviewer_id,"
                        " role, sort_key, etag)"
                        " select %s, id, %s, auditor_id, 'auditor', %s, %s"
                        " from screening_stage where id = %s",
                        (
                            c.rescreen.assignment_id,
                            c.rescreen.item_ref,
                            c.rescreen.sort_key,
                            _etag(),
                            c.stage_id,
                        ),
                    )
            for change, event in zip(c.conflicts, rest, strict=True):
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
            etag=row["etag"],
        )

    def _own_assignment(
        self, tx: Tx, project_id: Id, reviewer_id: Id, assignment_id: Id, *, lock: bool = False
    ) -> dict[str, Any]:
        a = store.assignment(tx, assignment_id, lock=lock)
        if a is None or a["reviewer_id"] != reviewer_id:
            raise NotAssigned(f"assignment {assignment_id} not found")
        store.stage_row(tx, project_id, a["stage_id"])
        return a

    def _after_decision(
        self,
        row: dict[str, Any],
        a: dict[str, Any],
        peers: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        reading: Reading,
        own_provisional: Decision,
        own_final: Decision,
    ) -> tuple[list[_ConflictChange], _Rescreen | None]:
        """The conflicts and the extra screen a settled decision calls for."""
        finals: list[tuple[str, Decision | None]] = []
        provisionals: list[Decision] = []
        for p in peers:
            if p["id"] == a["id"]:
                final, provisional = own_final, own_provisional
            else:
                final = store.decision_of(p, "f_") if p["f_outcome"] else None
                provisional = store.decision_of(p, "p_") if p["p_outcome"] else None
            finals.append((p["role"], final))
            if p["role"] == "screener" and provisional is not None:
                provisionals.append(provisional)
        screener_finals = [d for role, d in finals if role == "screener"]
        if any(d is None for d in screener_finals):
            return [], None
        decided = [d for d in screener_finals if d is not None]
        ref = a["item_ref"]
        rescreen = None
        if a["role"] == "screener" and not a["rescreen"]:
            reason = rescreen_reason(ScreeningMode(row["mode"]), decided, provisionals, reading)
            if reason is not None:
                has_auditor = any(p["role"] == "auditor" for p in peers)
                rescreen = _Rescreen(
                    item_ref=ref,
                    reason=reason,
                    assignment_id=None if has_auditor else _random_id(),
                    sort_key=sort_key(row["seed"], ref, str(row["auditor_id"])),
                )
        auditor = next((d for role, d in finals if role == "auditor"), None)
        kinds = conflict_kinds(row["stage"], decided, auditor)
        open_ = next((c for c in conflicts if c["state"] == "open"), None)
        if open_ is not None:
            # A new decision on an item in conflict (the auditor's) changes what adjudicators see.
            merged = tuple(dict.fromkeys((*open_["kinds"], *kinds)))
            return [_ConflictChange("update", open_["id"], ref, merged, _etag())], rescreen
        settled = {k for c in conflicts if c["state"] == "resolved" for k in c["kinds"]}
        new = tuple(k for k in kinds if k not in settled)
        if not new:
            return [], rescreen
        return [_ConflictChange("open", _random_id(), ref, new, _etag())], rescreen

    def _write_conflict(
        self, tx: Tx, change: _ConflictChange, stage_id: Id, event: AuditEvent
    ) -> None:
        if change.action == "open":
            tx.execute(
                "insert into screening_conflict (id, stage_id, item_ref, kinds, etag, state,"
                " opened_event_id, opened_at) values (%s, %s, %s, %s, %s, 'open', %s, %s)",
                (
                    change.conflict_id,
                    stage_id,
                    change.item_ref,
                    list(change.kinds),
                    change.etag,
                    event.event_id,
                    event.recorded_at,
                ),
            )
        elif change.action == "update":
            tx.execute(
                "update screening_conflict set kinds = %s, etag = %s, revision = revision + 1"
                " where id = %s",
                (list(change.kinds), change.etag, change.conflict_id),
            )
        else:
            tx.execute(
                "update screening_conflict set state = 'withdrawn', revision = revision + 1,"
                " etag = %s, closed_event_id = %s where id = %s",
                (_etag(), event.event_id, change.conflict_id),
            )

    def undo(
        self,
        tx: Tx,
        *,
        command_id: Id,
        project_id: Id,
        reviewer_id: Id,
        assignment_id: Id,
        expected_etag: str,
    ) -> UndoResult:
        """Take back the reviewer's decision on an item; it returns to the queue with a new
        token. The decisions stay in the record. An open conflict on the item is withdrawn;
        an adjudicated item cannot be undone."""
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.undo",
            actor_id=reviewer_id,
            payload={"assignment_id": str(assignment_id), "expected_etag": expected_etag},
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            a = self._own_assignment(tx, project_id, reviewer_id, assignment_id, lock=True)
            return {"a": a, "conflicts": store.conflicts(tx, a["stage_id"], a["item_ref"])}

        def decide(_: Command, st: dict[str, Any]) -> RunDecision[dict[str, Any]]:
            a = st["a"]
            if a["etag"] != expected_etag:
                raise StaleRevision("This item changed since you loaded it. Reload the queue.")
            if a["state"] == "pending":
                raise InvalidDecision("There is no decision to undo.")
            _refuse_withdrawn(a)
            if any(c["state"] == "resolved" for c in st["conflicts"]):
                raise InvalidDecision("This item was adjudicated. Its decisions stand.")
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
            events += [
                _conflict_event(project_id, reviewer_id, a["stage_id"], c) for c in withdrawn
            ]
            return RunDecision(
                events=tuple(events),
                changes={
                    "undone": undone,
                    "revision": revision,
                    "etag": _etag(),
                    "withdrawn": withdrawn,
                    "stage_id": a["stage_id"],
                },
            )

        def apply(tx: Tx, result: RunDecision[dict[str, Any]], events: tuple[AuditEvent, ...]):
            c = result.changes
            assert c is not None
            tx.execute(
                "insert into screening_undo (id, assignment_id, command_id, event_id,"
                " undone_decision_ids, revision, etag, recorded_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    new_id(),
                    assignment_id,
                    command_id,
                    events[0].event_id,
                    c["undone"],
                    c["revision"],
                    c["etag"],
                    events[0].recorded_at,
                ),
            )
            tx.execute(
                "update screening_assignment set state = 'pending', revision = %s, etag = %s,"
                " provisional_decision_id = null, final_decision_id = null where id = %s",
                (c["revision"], c["etag"], assignment_id),
            )
            for change, event in zip(c["withdrawn"], events[1:], strict=True):
                self._write_conflict(tx, change, c["stage_id"], event)

        self._commands.execute(tx, command, decide, load=load, apply=apply)
        row = tx.execute(
            "select u.etag, u.recorded_at, a.reviewer_id from screening_undo u"
            " join screening_assignment a on a.id = u.assignment_id where u.command_id = %s",
            (command_id,),
        ).fetchone()
        if row is None or row["reviewer_id"] != reviewer_id:
            raise NotAssigned(f"command {command_id} undid nothing of this reviewer")
        return UndoResult(etag=row["etag"], recorded_at=row["recorded_at"])

    def withdraw_item(
        self,
        tx: Tx,
        *,
        command_id: Id,
        project_id: Id,
        reviewer_id: Id,
        assignment_id: Id,
        expected_etag: str,
        reason: WithdrawReason,
    ) -> WithdrawResult:
        """Take a record out of full-text screening: the full text shown on one of the reviewer's
        own assignments is not the record's report.

        The full text belongs to the record, not to a scope, so the record leaves every
        full-text stage of the project that holds it, each with its own
        `screening.item_withdrawn` event. It leaves every queue, and no count, outcome, agreement
        figure, or audit stratum includes it. This is not an exclusion. The caller detaches the
        document from the record in the same transaction, so the record is back to having no
        full text.

        Only from an assignment the reviewer has not decided, not even provisionally (a reveal
        must not be escaped this way), and only while nobody has decided the record in any of
        those stages: a decision someone made stands.
        """
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.withdraw_item",
            actor_id=reviewer_id,
            payload={
                "assignment_id": str(assignment_id),
                "expected_etag": expected_etag,
                "reason": reason,
            },
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            a = self._own_assignment(tx, project_id, reviewer_id, assignment_id, lock=True)
            stages = tx.execute(
                "select i.stage_id from screening_item i"
                " join screening_stage s on s.id = i.stage_id"
                " where s.project_id = %s and s.stage = 'full_text' and i.item_ref = %s"
                " and i.withdrawn_event_id is null order by s.opened_at, s.id",
                (project_id, a["item_ref"]),
            ).fetchall()
            return {
                "a": a,
                "stage": store.stage_row(tx, project_id, a["stage_id"]),
                "stages": {
                    r["stage_id"]: (
                        store.item_assignments(tx, r["stage_id"], a["item_ref"]),
                        store.conflicts(tx, r["stage_id"], a["item_ref"]),
                    )
                    for r in stages
                },
            }

        def decide(_: Command, st: dict[str, Any]) -> RunDecision[_Withdrawal]:
            a, row = st["a"], st["stage"]
            if a["etag"] != expected_etag:
                raise StaleRevision("This item changed since you loaded it. Reload the queue.")
            if row["stage"] != "full_text":
                raise InvalidDecision("Only a full text can be the wrong document.")
            _refuse_withdrawn(a)
            if a["state"] != "pending":
                raise InvalidDecision(
                    "You decided this report already, so it stays in screening. Keep or change"
                    " your decision."
                )
            for peers, conflicts in st["stages"].values():
                if any(p["state"] != "pending" for p in peers) or any(
                    c["state"] != "withdrawn" for c in conflicts
                ):
                    raise InvalidDecision(
                        "Someone already decided this report, so it stays in screening. Tell the"
                        " methods lead that the PDF is wrong."
                    )
            events = tuple(
                _event(
                    project_id,
                    ITEM_WITHDRAWN,
                    reviewer_id,
                    {
                        "stage_id": str(stage_id),
                        "item_ref": a["item_ref"],
                        "assignment_id": str(assignment_id),
                        "reason": reason,
                    },
                )
                for stage_id in st["stages"]
            )
            stages = tuple(
                (sid, tuple(p["id"] for p in peers)) for sid, (peers, _) in st["stages"].items()
            )
            return RunDecision(events=events, changes=_Withdrawal(a["item_ref"], stages))

        def apply(tx: Tx, result: RunDecision[_Withdrawal], events: tuple[AuditEvent, ...]):
            w = result.changes
            assert w is not None
            for (stage_id, assignments), event in zip(w.stages, events, strict=True):
                tx.execute(
                    "update screening_item set withdrawn_reason = %s, withdrawn_event_id = %s"
                    " where stage_id = %s and item_ref = %s",
                    (reason, event.event_id, stage_id, w.item_ref),
                )
                # New tokens: a client still holding the item can no longer decide it.
                for assignment in assignments:
                    tx.execute(
                        "update screening_assignment set etag = %s, revision = revision + 1"
                        " where id = %s",
                        (_etag(), assignment),
                    )

        receipt = self._commands.execute(tx, command, decide, load=load, apply=apply)
        return WithdrawResult(recorded_at=receipt.accepted_at)

    def own_reveal(
        self, tx: Tx, project_id: Id, reviewer_id: Id, assignment_id: Id
    ) -> Reveal | None:
        """The AI reading shown to the reviewer after their own provisional decision on the
        assignment, or None if none was shown. Never anything the reviewer has not seen."""
        a = self._own_assignment(tx, project_id, reviewer_id, assignment_id)
        if not a["revealed"] or a["provisional_decision_id"] is None:
            return None
        row = tx.execute(
            "select reveal from screening_decision where id = %s", (a["provisional_decision_id"],)
        ).fetchone()
        return Reveal.model_validate(row["reveal"]) if row and row["reveal"] else None

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
        pseudonym. Every one is a disagreement between humans, so being in the queue says nothing
        about the AI; its column opens only after the adjudicator commits."""
        row = store.find_stage(tx, project_id, scope_id, stage, topic_id)
        conflicts = _adjudicable(tx, row, adjudicator_id)
        criteria = self._criteria(tx, row)
        return [
            AdjudicationItem(
                conflict_id=c["id"],
                etag=c["etag"],
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
        expected_etag: str,
        adopted: str | None = None,
        decision: Decision | None = None,
        timing: DecisionTiming | None = None,
    ) -> AdjudicationResult:
        """Resolve a conflict by adopting a column (`adopted="A"`) or with an own decision.

        The result opens the AI column. The adjudicator must be a human who did not screen the
        record."""
        if (adopted is None) == (decision is None):
            raise InvalidDecision("Adopt one column, or give your own decision.")
        command = Command(
            command_id=command_id,
            project_id=project_id,
            type="screening.adjudicate",
            actor_id=adjudicator_id,
            payload={
                "conflict_id": str(conflict_id),
                "expected_etag": expected_etag,
                "adopted": adopted,
                "decision": _json(decision),
            },
        )

        def load(tx: Tx, _: Command) -> dict[str, Any]:
            c = tx.execute(
                "select * from screening_conflict where id = %s for update", (conflict_id,)
            ).fetchone()
            if c is None:
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
            if c["etag"] != expected_etag:
                raise StaleRevision("This conflict changed since you loaded it. Reload the queue.")
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
                " etag = %s, closed_event_id = %s where id = %s",
                (_etag(), events[0].event_id, conflict_id),
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
        item = store.item(tx, row["stage_id"], row["item_ref"])
        spans = {
            c.criterion_id: span
            for c in reading.criteria
            if c.quote_verified
            and (span := locate_quote(c.quote, item["title"], item["text"])) is not None
        }
        return AdjudicationResult(
            outcome=row["outcome"],
            reason_criterion_id=row["reason_criterion_id"],
            recorded_at=row["recorded_at"],
            ai=AiColumn(excludes=reading.excludes, criteria=reading.criteria, quote_spans=spans),
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
        records. A record waiting for its extra screen is not yet screened. For the lead and later
        stages, never
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
        counts; an adjudicator the progress and their conflicts. The whole stage (its agreement,
        the audit gauge, and the M3 stop test) is for a methods lead who takes no part in any
        stage (`sees_lead_data`). Anyone else, another reviewer of the project included, is not
        assigned."""
        row = store.find_stage(tx, project_id, scope_id, stage, topic_id)
        viewer = tx.execute("select kind from actor where id = %s", (viewer_id,)).fetchone()
        if viewer is None or viewer["kind"] != "human":
            raise NotAssigned(f"actor {viewer_id} is not a human reviewer")
        if (
            viewer_id in row["adjudicators"]
            and viewer_id not in row["screeners"]
            and viewer_id != row["auditor_id"]
        ):
            # Their pending adjudications and the screening progress; nothing of the AI.
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
        if not store.sees_lead_data(tx, project_id, viewer_id):
            raise NotAssigned(f"actor {viewer_id} has no part in this stage")
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


def _refuse_withdrawn(a: dict[str, Any]) -> None:
    if a["withdrawn_event_id"] is not None:
        raise InvalidDecision("This report was taken out of screening as the wrong document.")


def _adjudicable(tx: Tx, row: dict[str, Any], adjudicator_id: Id) -> list[dict[str, Any]]:
    """The open conflicts an adjudicator of the stage may see: on records they did not review, in
    an order hashed from random IDs."""
    if adjudicator_id not in row["adjudicators"]:
        raise NotAssigned(f"actor {adjudicator_id} does not adjudicate this stage")
    return tx.execute(
        "select c.id, c.item_ref, c.etag, i.title, i.text, i.facts"
        " from screening_conflict c"
        " join screening_item i on i.stage_id = c.stage_id and i.item_ref = c.item_ref"
        " where c.stage_id = %s and c.state = 'open'"
        " and not exists (select 1 from screening_assignment a where a.stage_id = c.stage_id"
        " and a.item_ref = c.item_ref and a.reviewer_id = %s)"
        " order by md5(c.id::text)",
        (row["id"], adjudicator_id),
    ).fetchall()


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
