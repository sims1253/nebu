"""Human screening: queues, blinding, reveal, sentinels, adjudication (DESIGN.md, "Screening").

The workbench is a thin client of this module. The human decides first; the AI appears only on
disagreement; the backend enforces blinding; audit items and sentinels are invisible to reviewers.

Owner: WP-09. Shipped now: the types and the service interface. Still to build: the mode rules
(M1 to M3), assignment and blinding, reveal-after-commit with provisional and final decisions,
sentinel and audit sampling, conflict detection, and adjudication.
"""

from datetime import datetime
from typing import Literal, Protocol

from kittum.core import Id, Record
from kittum.protocol import Stage

type Outcome = Literal["include", "exclude", "unclear", "insufficient_information"]
type Phase = Literal["provisional", "final"]


class QueueItem(Record):
    """What a reviewer sees. Never contains AI output, sentinel flags, or the other reviewer."""

    assignment_id: Id
    stage: Stage
    scope_id: str
    title: str
    text: str
    highlights: tuple[tuple[int, int, str], ...] = ()  # protocol terms and entity tags
    revision: int


class Decision(Record):
    outcome: Outcome
    reason_criterion_id: str | None = None
    evidence_anchor_ids: tuple[Id, ...] = ()
    comment: str | None = None


class Reveal(Record):
    """Shown after a provisional decision when the AI disagrees."""

    criterion_id: str
    quote: str
    rationale: str


class DecideResult(Record):
    phase: Phase
    reveal: Reveal | None
    recorded_at: datetime


class ScopeStatus(Record):
    scope_id: str
    stage: Stage
    screened: int
    remaining: int
    conflicts: int


class ScreeningService(Protocol):
    def next(self, reviewer_id: Id, scope_id: str, stage: Stage) -> QueueItem | None: ...

    def decide(
        self, assignment_id: Id, decision: Decision, *, phase: Phase, expected_revision: int
    ) -> DecideResult: ...

    def status(self, scope_id: str, stage: Stage) -> ScopeStatus: ...


__all__ = [
    "DecideResult",
    "Decision",
    "Outcome",
    "Phase",
    "QueueItem",
    "Reveal",
    "ScopeStatus",
    "ScreeningService",
]
