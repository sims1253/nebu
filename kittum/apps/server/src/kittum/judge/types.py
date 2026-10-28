"""The judge's values: what it reads, how it is configured, and what it answers."""

from typing import Literal

from kittum.core import Record, Sha256
from kittum.protocol import Stage

type CriterionStatus = Literal["met", "not_met", "unclear"]

type FailedCheck = Literal[
    "quote_not_found",  # the quote is not in the record
    "no_quote",  # not_met without a quote
    "off_criterion",  # the quote contains no key term of the condition
    "negation",  # a negation or exclusion cue whose scope is not the key term
    "polarity",  # the quote does not contradict an inclusion or affirm an exclusion condition
    "language_unverifiable",  # the criterion or the quote is not positively English or German
    "multiple_sentences",  # the quote spans a sentence boundary, so its scope cannot be read
    "injection_suspected",  # the record contains text that tries to instruct the model
    "invalid_output",  # the model gave no valid answer in any attempt
]


class ScreeningItem(Record):
    """What the judge reads: one record at title and abstract, or one parsed full text."""

    item_ref: str
    stage: Stage
    title: str
    text: str

    @property
    def source(self) -> str:
        """The text quotes are checked against."""
        return f"{self.title}\n\n{self.text}"


class CriterionVerdict(Record):
    criterion_id: str
    status: CriterionStatus
    quote: str | None
    rationale: str
    quote_verified: bool
    downgraded_from: CriterionStatus | None = None
    failed_check: FailedCheck | None = None


class JudgeConfig(Record):
    id: str
    model_id: str  # a recipe ID from `kittum.models.ModelRegistry`
    prompt_version: str
    temperature: float = 0.0
    seed: int = 0  # attempt n uses seed + n
    max_tokens: int = 4096
    max_attempts: int = 2  # tries before a malformed answer is recorded as unclear


class Verdict(Record):
    item_ref: str
    scope_id: str
    judge_config_id: str
    criteria: tuple[CriterionVerdict, ...]
    raw_output_hash: Sha256  # SHA-256 of the final completion text
    model_identity: str | None = None
    flags: tuple[str, ...] = ()  # "injection_suspected", "invalid_output"

    @property
    def excludes(self) -> bool:
        return any(
            c.status == "not_met" and c.quote_verified and c.failed_check is None
            for c in self.criteria
        )
