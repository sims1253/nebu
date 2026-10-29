"""Human screening: queues, blinding, reveal, sentinels, audits, adjudication (DESIGN.md,
"Screening modes", "Screening and the AI judge", and "Reviewer workbench").

The protocol's screening mode for a search topic decides who screens each scope and stage: two
humans in M1 and M3, one in M2. `Screening.open_stage` creates the items and assignments, seeds
sentinels (records with known labels) into every reviewer's queue and gives a random audit
sample to an auditor, both at the protocol's percentages and invisible to reviewers.
`request_judging` enqueues judge jobs, and `judge_handler` runs them in a `kittum.runs.Worker`
and attaches each verdict as the stage's AI reading. An item enters reviewers' queues once the
AI has read it: M1 and M2 queues follow a seeded order per reviewer, M3 queues the AI's order,
shared by both humans.

The human decides first. `decide` takes a provisional decision; if the stage reveals (`never`,
`on_disagreement`, or `always`) and the AI disagrees in the sense below, the result carries the
AI's criterion, quote, and rationale, and the reviewer keeps or revises with a final decision.
Otherwise the provisional decision is final at once. Every decision is a command with a receipt
and a ledger event, checks the opaque `etag` the client last saw (a random token per state, so
it reveals no count of decisions), and records the client's timing; `undo` returns an item to
the queue under a new token. AI exclusions are proposals only: no mode lets the AI exclude a
record that no human saw.

The AI's verdict is read two ways. Its verified reading (`Verdict.excludes`) counts wherever an
AI exclusion would remove or deprioritize a record: when the human includes, in the M3 order,
and in the audit strata. The model's own reading (no criterion it answered `not_met`, before the
filter's downgrades) counts where the AI argues for keeping a record that humans excluded. The
filter protects against wrongful exclusions; it never manufactures a disagreement.

Once every screener of a record has decided, humans who disagree go to adjudication. An AI-only
disagreement never does: records both humans exclude while the model saw no failing criterion
(M1), and every disagreement between the AI and the lone human (M2, provisional or final, the
protocol's census), get a blind extra screen by the stage's auditor instead, mixed into the
auditor's queue among the random audit samples (`screening.ai_disagreement_rescreen`). If that
screen agrees with the screeners, their decision stands; if not, the human disagreement goes to
adjudication. So adjudication holds only human disagreements, and the adjudicator, who sees the
humans' decisions under pseudonyms, learns nothing about the AI from the queue; the AI column
opens after the commit.

Blinding is enforced below the service: reviewers read only through the SQL functions
`screening_reviewer_queue` and `screening_reviewer_progress`, which return the reviewer's own
assignments without AI output (until their own provisional decision is revealed), sentinel or
audit flags, or anything of another reviewer. `status` shows screeners and the auditor only their
own counts, adjudicators their pending adjudications, and the methods lead the whole stage:
conflicts, the extra screens and their outcomes, kappa and PABAK for human pairs and human-AI
pairs, the audit gauge (sentinel accuracy and the elusion estimate), and the M3 stop test.

Full-text outcomes are include, exclude, and insufficient information. An exclusion names the
criterion the report fails, read `not_met` with evidence anchor IDs from `kittum.documents` (held
as opaque IDs); an inclusion says every criterion is met and needs no reading of each; insufficient
information names an `unclear` criterion. Readings of the other criteria are optional. A reviewer
who finds that the full text shown is not the record's report withdraws the item
(`withdraw_item`): it leaves every queue, count, and outcome without a decision, which is not an
exclusion.

Owner: WP-09.
"""

from kittum.screening.jobs import JudgeRunner, judge_handler
from kittum.screening.metrics import RecordOutcome, stop_status
from kittum.screening.rules import criteria_for, topic_for
from kittum.screening.service import (
    ADJUDICATED,
    AI_DISAGREEMENT_RESCREEN,
    AI_READ,
    CONFLICT_OPENED,
    CONFLICT_UPDATED,
    CONFLICT_WITHDRAWN,
    DECIDED,
    ITEM_WITHDRAWN,
    JUDGE_JOB,
    JUDGING_REQUESTED,
    STAGE_OPENED,
    UNDONE,
    Screening,
)
from kittum.screening.store import sees_lead_data, undecided_items
from kittum.screening.types import (
    AdjudicationItem,
    AdjudicationResult,
    AiColumn,
    AuditGauge,
    AuditStratum,
    ConflictKind,
    CriterionDecision,
    DecideResult,
    Decision,
    DecisionTiming,
    Highlight,
    InvalidDecision,
    NotAssigned,
    Outcome,
    PairAgreement,
    Phase,
    QueueItem,
    QueueRecord,
    QuoteSpan,
    RecordFacts,
    RescreenGauge,
    RescreenReason,
    Reveal,
    RevealSetting,
    ReviewerColumn,
    ScopeStatus,
    ScreeningRecord,
    Sentinel,
    StageInfo,
    StageMetrics,
    StageSpec,
    StaleRevision,
    StopStatus,
    UndoResult,
    WithdrawReason,
    WithdrawResult,
)

__all__ = [
    "ADJUDICATED",
    "AI_DISAGREEMENT_RESCREEN",
    "AI_READ",
    "CONFLICT_OPENED",
    "CONFLICT_UPDATED",
    "CONFLICT_WITHDRAWN",
    "DECIDED",
    "ITEM_WITHDRAWN",
    "JUDGE_JOB",
    "JUDGING_REQUESTED",
    "STAGE_OPENED",
    "UNDONE",
    "AdjudicationItem",
    "AdjudicationResult",
    "AiColumn",
    "AuditGauge",
    "AuditStratum",
    "ConflictKind",
    "CriterionDecision",
    "DecideResult",
    "Decision",
    "DecisionTiming",
    "Highlight",
    "InvalidDecision",
    "JudgeRunner",
    "NotAssigned",
    "Outcome",
    "PairAgreement",
    "Phase",
    "QueueItem",
    "QueueRecord",
    "QuoteSpan",
    "RecordFacts",
    "RecordOutcome",
    "RescreenGauge",
    "RescreenReason",
    "Reveal",
    "RevealSetting",
    "ReviewerColumn",
    "ScopeStatus",
    "Screening",
    "ScreeningRecord",
    "Sentinel",
    "StageInfo",
    "StageMetrics",
    "StageSpec",
    "StaleRevision",
    "StopStatus",
    "UndoResult",
    "WithdrawReason",
    "WithdrawResult",
    "criteria_for",
    "judge_handler",
    "sees_lead_data",
    "stop_status",
    "topic_for",
    "undecided_items",
]
