"""The values screening reads and answers, and its errors."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from kittum.core import Id, KittumError, Record
from kittum.judge import CriterionStatus, CriterionVerdict
from kittum.protocol import ScreeningMode, Stage
from kittum.runs import CommandRejected
from kittum.stats import Interval

type Outcome = Literal["include", "exclude", "unclear", "insufficient_information"]
type Phase = Literal["provisional", "final"]
type RevealSetting = Literal["never", "on_disagreement", "always"]
type ConflictKind = Literal[
    "human_disagreement",  # the screeners disagree on inclusion
    "exclusion_reason",  # at full text, both exclude on different criteria
    "audit_disagreement",  # the auditor's screen disagrees with the screeners
]
"""Why a record goes to adjudication. Only human decisions disagree here: an AI-only disagreement
becomes a blind extra screen in the audit stream instead (`RescreenReason`)."""
type RescreenReason = Literal[
    "ai_include",  # M1: the model saw no failing criterion, both humans excluded
    "ai_disagreement",  # M2: the AI disagrees with the lone human
]
type Highlight = tuple[int, int, str]
"""(start, end, tag): half-open code-point offsets; tags are `protocol:<criterion id>`."""


class StaleRevision(CommandRejected):
    """`expected_revision` is not the current revision: someone or something changed the item."""


class InvalidDecision(CommandRejected):
    """The decision breaks a rule: a missing criterion, a wrong phase, or an outcome of another
    stage."""


class NotAssigned(KittumError):
    """The assignment or conflict does not exist for this reviewer. Reported as not found."""


# Inputs


class RecordFacts(Record):
    """Bibliographic facts shown above the abstract."""

    pmid: str | None = None
    doi: str | None = None
    journal: str | None = None
    year: int | None = None
    publication_types: tuple[str, ...] = ()
    registry_ids: tuple[str, ...] = ()


class ScreeningRecord(Record):
    """One item to screen: a report at title and abstract, or its parsed full text.

    `item_ref` names the record across modules (the corpus report ID); the judge's verdicts are
    keyed by it.
    """

    item_ref: str
    title: str
    text: str
    facts: RecordFacts = RecordFacts()


class Sentinel(Record):
    """A record with a known label, seeded invisibly into every screener's queue."""

    record: ScreeningRecord
    expected_outcome: Literal["include", "exclude"] = "include"


class StageSpec(Record):
    """What opening a stage needs. The protocol version decides the mode and the audit plan."""

    protocol_version_id: Id
    scope_id: str
    stage: Stage
    topic_id: str | None = None  # needed only when the scope is in several topics
    records: tuple[ScreeningRecord, ...] = Field(min_length=1)
    screeners: tuple[Id, ...] = Field(min_length=1)
    auditor_id: Id
    """Screens the random audit sample and the blind extra screens of AI-only disagreements; never
    one of the screeners."""
    adjudicators: tuple[Id, ...] = Field(min_length=1)
    """Who adjudicates the stage's conflicts. They see the adjudicator's view of `status`."""
    sentinels: tuple[Sentinel, ...] = ()
    reveal: RevealSetting = "on_disagreement"
    judge_config_id: str
    seed: str | None = None  # defaults to one derived from the protocol hash, scope, and stage


class StageInfo(Record):
    stage_id: Id
    project_id: Id
    scope_id: str
    stage: Stage
    topic_id: str
    mode: ScreeningMode
    reveal: RevealSetting
    protocol_hash: str
    judge_config_id: str
    screeners: tuple[Id, ...]
    auditor_id: Id
    adjudicators: tuple[Id, ...]


# The reviewer's side


class CriterionDecision(Record):
    """A reviewer's reading of one criterion at full text, with the evidence for it."""

    criterion_id: str
    status: CriterionStatus
    evidence_anchor_ids: tuple[Id, ...] = ()  # anchors from `kittum.documents`, opaque here


class Decision(Record):
    outcome: Outcome
    reason_criterion_id: str | None = None
    evidence_anchor_ids: tuple[Id, ...] = ()
    comment: str | None = None
    criteria: tuple[CriterionDecision, ...] = ()  # full text: one per criterion


class DecisionTiming(Record):
    """Client-side timing of one decision (DESIGN.md, "Metrics")."""

    shown_at: datetime
    first_key_at: datetime | None = None
    decided_at: datetime
    reveal_shown_at: datetime | None = None


class Reveal(Record):
    """Shown after a provisional decision when the AI disagrees (or always, if set so)."""

    criterion_id: str
    status: CriterionStatus
    quote: str
    rationale: str


class QueueRecord(Record):
    """The record's facts as the queue shows them, with title highlights and abstract sections."""

    pmid: str | None
    doi: str | None
    journal: str | None
    year: int | None
    publication_types: tuple[str, ...]
    registry_ids: tuple[str, ...]
    title_highlights: tuple[Highlight, ...]
    sections: tuple[tuple[str, int, int], ...]
    """Structured-abstract labels as (label, start, end) code-point ranges of `text`."""


class QueueItem(Record):
    """What a reviewer sees. Never contains AI output before their own provisional decision,
    sentinel or audit flags, or anything of another reviewer."""

    assignment_id: Id
    stage: Stage
    scope_id: str
    title: str
    text: str
    highlights: tuple[Highlight, ...]
    etag: str
    """An opaque token for the item's current state; send it back as `expected_etag`."""
    record: QueueRecord
    phase: Phase
    """The phase the next decision must have: `final` after a reveal of this reviewer's own
    provisional decision, which then comes back in `reveal`."""
    reveal: Reveal | None


class DecideResult(Record):
    phase: Phase
    """`final` when no reveal follows: the decision stands. `provisional` with a reveal: the
    reviewer keeps or revises with a `final` decision."""
    reveal: Reveal | None
    recorded_at: datetime
    etag: str


class UndoResult(Record):
    etag: str
    recorded_at: datetime


# Adjudication


class ReviewerColumn(Record):
    """One human's final decision, pseudonymized as A, B, ..."""

    label: str
    outcome: Outcome
    reason_criterion_id: str | None
    criteria: tuple[CriterionDecision, ...]
    evidence_anchor_ids: tuple[Id, ...]
    comment: str | None


class AdjudicationItem(Record):
    """A conflict for the adjudicator. The AI column is not here; it opens after the commit."""

    conflict_id: Id
    etag: str
    stage: Stage
    scope_id: str
    title: str
    text: str
    record: QueueRecord
    columns: tuple[ReviewerColumn, ...]


class AiColumn(Record):
    excludes: bool
    criteria: tuple[CriterionVerdict, ...]


class AdjudicationResult(Record):
    outcome: Outcome
    reason_criterion_id: str | None
    recorded_at: datetime
    ai: AiColumn


# Status


class PairAgreement(Record):
    """Agreement on include against exclude, from the blind provisional decisions.

    `human_ai` compares with the model's own reading (no criterion it answered `not_met`);
    `human_ai_verified` with the reading the judge's filter verified (`Verdict.excludes`).
    """

    kind: Literal["human_human", "human_ai", "human_ai_verified"]
    raters: tuple[str, str]  # reviewer IDs, or "ai"
    items: int
    kappa: float | None  # None when undefined: no items, or one label throughout
    pabak: float | None


class AuditStratum(Record):
    """Excluded records whose AI reading was, or was not, a verified exclusion."""

    ai_excludes: bool
    population: int
    sampled: int  # decided audit samples in the stratum
    relevant_found: int


class AuditGauge(Record):
    sentinels: int  # sentinel assignments
    sentinels_decided: int
    sentinels_correct: int
    audit_samples: int  # records sampled for audit
    audit_decided: int
    excluded_population: int
    """Records excluded without adjudication (plus, in M3, every record not yet screened)."""
    audit_relevant_found: int
    strata: tuple[AuditStratum, ...]
    elusion: Interval | None
    """Estimated relevant records among the excluded ones, stratified by the verified AI
    reading, with its one-sided 95% upper bound. None while a stratum with records has no
    decided audit sample."""


class StopStatus(Record):
    screened: int  # the fully screened prefix in AI order
    relevant: int
    remaining: int
    target_recall: float
    alpha: float
    p_value: float
    minimum_screened_percent: str
    minimum_met: bool
    pending_conflicts: int  # unresolved conflicts in the tested prefix
    verdict: Literal["stop", "continue", "pending_conflicts"]
    """`stop` when the test rejects, the minimum is met, and no conflict in the prefix is open;
    `pending_conflicts` withholds certification until they are adjudicated."""
    stop: bool  # verdict == "stop"
    method: str


class RescreenGauge(Record):
    """Blind extra screens that AI-only disagreements sent to the auditor. Lead's view only."""

    triggered: int
    pending: int
    upheld: int  # the auditor agreed with the screeners: their decision stands
    adjudicated: int  # the auditor disagreed: a human disagreement went to adjudication


class StageMetrics(Record):
    mode: ScreeningMode
    reveal: RevealSetting
    conflicts_resolved: int
    reveals_shown: int
    revised_after_reveal: int
    agreement: tuple[PairAgreement, ...]
    audit: AuditGauge
    rescreens: RescreenGauge
    stop_test: StopStatus | None  # M3 only


class ScopeStatus(Record):
    """Progress of a scope and stage.

    A reviewer who screens or audits the stage sees only their own counts (`view == "reviewer"`,
    hidden items included, no conflicts, no metrics). An adjudicator of the stage sees the
    screening progress and, as `conflicts`, the adjudications pending for them, without
    metrics. Anyone else (the methods lead) sees the whole stage.
    """

    scope_id: str
    stage: Stage
    view: Literal["reviewer", "adjudicator", "lead"]
    screened: int
    remaining: int
    conflicts: int | None
    metrics: StageMetrics | None
