"""The AI judge for screening (DESIGN.md, "Screening and the AI judge").

The judge answers each criterion as `met`, `not_met`, or `unclear` with a verbatim quote. An
exclusion counts only if a `not_met` quote is found in the record by `kittum.core.find_quote` and
passes the criterion's keyword and negation checks. Anything else becomes `unclear`, and
`unclear` at title and abstract means include.

Owner: WP-08. Shipped now: the types. Still to build: prompts, schema-constrained calls through
`kittum.models.LlmBackend`, quote and negation checks, raw output capture, and the gold-data
metrics (per-judge recall, double-fault rate, run-to-run kappa).
"""

from typing import TYPE_CHECKING, Literal

from kittum.core import Record, Sha256
from kittum.protocol import Criterion, Stage

if TYPE_CHECKING:
    from kittum.models import LlmBackend

type CriterionStatus = Literal["met", "not_met", "unclear"]


class ScreeningItem(Record):
    """What the judge reads: one record at title and abstract, or one parsed full text."""

    item_ref: str
    stage: Stage
    title: str
    text: str


class CriterionVerdict(Record):
    criterion_id: str
    status: CriterionStatus
    quote: str | None
    rationale: str
    quote_verified: bool
    downgraded_from: CriterionStatus | None = None


class JudgeConfig(Record):
    id: str
    model_id: str
    prompt_version: str


class Verdict(Record):
    item_ref: str
    scope_id: str
    judge_config_id: str
    criteria: tuple[CriterionVerdict, ...]
    raw_output_hash: Sha256

    @property
    def excludes(self) -> bool:
        return any(c.status == "not_met" and c.quote_verified for c in self.criteria)


def judge(
    item: ScreeningItem,
    scope_id: str,
    criteria: tuple[Criterion, ...],
    config: JudgeConfig,
    backend: LlmBackend,
) -> Verdict:
    raise NotImplementedError("WP-08")


__all__ = [
    "CriterionStatus",
    "CriterionVerdict",
    "JudgeConfig",
    "ScreeningItem",
    "Verdict",
    "judge",
]
