"""The protocol schema (DESIGN.md, "Protocol").

    ReviewProtocol (profile, extensions)
        ReviewQuestion: population, intervention, comparators, outcomes
        Outcome, EffectModifier
        SearchTopic
            Criterion, SourcePlan, ReferenceSet, ScreeningMode with AuditPlan and
            StoppingPlan, FreshnessPolicy

The schema knows nothing about any regulator or assessment body. A profile (see
`kittum.protocol.profile`) adds vocabularies, labels, validation rules, and typed `extensions` on
the protocol, questions, comparators, outcomes, topics, and criteria.

Every value is a frozen `Record`, so a typo in a source file fails as an unknown field. The order of
every tuple is part of the content hash: authors control it, and Kittum never reorders it.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, NoReturn

from pydantic import AfterValidator, Field, StringConstraints

from kittum.core import Record

type Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]
type Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type LanguageCode = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")]


class FrozenDict(dict[str, Any]):
    """A dict that cannot change, so a frozen record cannot change through a nested mapping."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        raise TypeError("protocol mappings are immutable")

    __setitem__ = __delitem__ = __ior__ = _immutable
    clear = pop = popitem = setdefault = update = _immutable

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> FrozenDict:
        return self

    def __reduce__(self) -> tuple[type[FrozenDict], tuple[dict[str, Any]]]:
        return FrozenDict, (dict(self),)


def deep_freeze(value: Any) -> Any:
    """Mappings become `FrozenDict`s and lists become tuples, all the way down."""
    if isinstance(value, dict):
        return FrozenDict({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(deep_freeze(v) for v in value)
    return value


type LocalizedText = Annotated[
    dict[LanguageCode, Text], Field(min_length=1), AfterValidator(deep_freeze)
]
"""Text in one or more languages, e.g. `{en: ..., de: ...}`. It must include the default language."""
type Extensions = Annotated[dict[str, Any], AfterValidator(deep_freeze)]
"""Profile-defined fields. Empty unless the protocol names a profile that defines them."""


def _percent_between(low: Decimal, high: Decimal, *, low_open: bool, high_open: bool) -> Any:
    def check(value: str) -> str:
        number = Decimal(value)
        if (
            number < low
            or number > high
            or (low_open and number == low)
            or (high_open and number == high)
        ):
            left, right = "(" if low_open else "[", ")" if high_open else "]"
            raise ValueError(f"must be in {left}{low}, {high}{right}")
        return value

    return AfterValidator(check)


_DECIMAL = StringConstraints(pattern=r"^[0-9]{1,3}(\.[0-9]+)?$")
_0, _100 = Decimal(0), Decimal(100)
type Percent = Annotated[str, _DECIMAL, _percent_between(_0, _100, low_open=False, high_open=False)]
"""A percentage as an exact decimal string, from 0 to 100 inclusive."""
type PositivePercent = Annotated[
    str, _DECIMAL, _percent_between(_0, _100, low_open=True, high_open=False)
]
"""Above 0, up to 100: a recall target."""
type ConfidencePercent = Annotated[
    str, _DECIMAL, _percent_between(_0, _100, low_open=True, high_open=True)
]
"""Strictly between 0 and 100: a confidence level."""

type PicoSlot = Literal[
    "population", "intervention", "comparator", "outcome", "study_type", "other"
]
type Stage = Literal["title_abstract", "full_text"]
type Polarity = Literal["include", "exclude"]


class ScreeningMode(StrEnum):
    M1 = "M1"  # two humans, AI as third screener
    M2 = "M2"  # one human, AI as second screener
    M3 = "M3"  # prioritized dual screening with a certified stop


class Comparator(Record):
    id: Identifier
    text: LocalizedText
    extensions: Extensions = Field(default_factory=dict)


class ReviewQuestion(Record):
    """One question or population of the review. Decisions are keyed by it as a scope."""

    id: Identifier
    label: LocalizedText
    population: LocalizedText
    intervention: LocalizedText
    comparators: tuple[Comparator, ...] = ()
    comparators_are_alternatives: bool = False
    """True when any one comparator answers the question; False for a comparison per comparator."""
    outcomes: tuple[Identifier, ...] = ()  # outcome IDs; empty means every outcome
    extensions: Extensions = Field(default_factory=dict)


class Outcome(Record):
    id: Identifier
    category: Identifier  # from the profile's "outcome_category" vocabulary, if it has one
    name: LocalizedText
    definition: LocalizedText | None = None
    rationale: LocalizedText | None = None
    extensions: Extensions = Field(default_factory=dict)


class EffectModifier(Record):
    id: Identifier
    label: LocalizedText
    categories: tuple[Text, ...] = ()


class Criterion(Record):
    """An executable eligibility criterion.

    `text` states the condition. For an `include` criterion a record must meet it; for an
    `exclude` criterion a record that shows it is excluded. Either way the judge answers whether
    the record *passes*: `not_met` with a verified quote is what excludes.
    """

    id: Identifier
    slot: PicoSlot
    polarity: Polarity = "include"
    text: LocalizedText
    decidable_at: tuple[Stage, ...] = Field(min_length=1)
    applies_to: tuple[str, ...] = Field(min_length=1)  # scope IDs
    keywords: tuple[Text, ...] = ()  # an exclusion quote must contain one of them, if any
    positive_examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()
    extensions: Extensions = Field(default_factory=dict)


type ScopeKind = Literal["question", "search_topic"]


class Scope(Record):
    """One review question or search topic that decisions are keyed by."""

    id: str
    label: str
    kind: ScopeKind = "question"


type SourceKind = Literal["bibliographic", "registry", "website", "other"]


class Source(Record):
    id: Identifier  # e.g. "medline", "central", "clinicaltrials_gov"
    kind: SourceKind
    name: Text
    platform: Text | None = None
    study_filter: Text | None = None
    strategy: Text | None = Field(default=None, exclude_if=lambda v: v is None)
    """The query as the source's own syntax writes it: PubMed field tags and MeSH, or
    ClinicalTrials.gov's Essie terms. Left out of the document when absent, so a protocol written
    before sources had strategies keeps its content hash."""


class Deviation(Record):
    """A justified departure from a required source plan, e.g. a source that cannot be searched."""

    source_id: Identifier
    justification: LocalizedText


class SourcePlan(Record):
    sources: tuple[Source, ...] = Field(min_length=1)
    deviations: tuple[Deviation, ...] = ()
    limits: Text | None = None  # language, date, or other limits, with their reason


type IdentifierKind = Literal["doi", "pmid", "nct", "isrctn", "eudract", "euct"]


class KnownInclude(Record):
    kind: IdentifierKind
    value: Text
    note: Text | None = None


class PressReview(Record):
    status: Literal["planned", "done"]
    reviewer: Text | None = None
    reviewed_on: date | None = None


class ReferenceSet(Record):
    """Known relevant records: the strategy is built on one set and validated on the other."""

    development: tuple[KnownInclude, ...] = ()
    validation: tuple[KnownInclude, ...] = ()
    press: PressReview | None = None


class AuditPlan(Record):
    sentinel_percent: Percent = "0"  # known includes seeded into the queue
    random_audit_percent: Percent = "0"  # human exclusions re-screened by a second person
    census_of_disagreements: bool = True  # every human-AI disagreement goes to adjudication


class StoppingPlan(Record):
    """When an M3 topic may stop screening in AI order."""

    method: Literal["buscar"]  # Callaghan and Müller-Hansen 2020
    target_recall_percent: PositivePercent
    confidence_percent: ConfidencePercent
    minimum_screened_percent: Percent


class FreshnessPolicy(Record):
    max_age_months: int = Field(ge=1)
    """How old the last search may be at `deadline`."""
    deadline: date | None = None


class SearchTopic(Record):
    id: Identifier
    kind: Identifier  # from the profile's "topic_kind" vocabulary, if it has one
    label: LocalizedText
    mode: ScreeningMode
    mode_rationale: LocalizedText | None = None  # required for M2 and M3
    validation_pack: Text | None = None  # required for M2
    scopes: tuple[Scope, ...] = Field(min_length=1)
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    source_plan: SourcePlan
    reference_set: ReferenceSet = ReferenceSet()
    audit: AuditPlan = AuditPlan()
    stopping: StoppingPlan | None = None  # required for M3, forbidden otherwise
    freshness: FreshnessPolicy
    extensions: Extensions = Field(default_factory=dict)


class ReviewProtocol(Record):
    schema_version: Literal[1] = 1
    version_label: Text
    title: LocalizedText
    default_language: LanguageCode
    profile: Identifier | None = None
    questions: tuple[ReviewQuestion, ...] = Field(min_length=1)
    outcomes: tuple[Outcome, ...] = Field(min_length=1)
    effect_modifiers: tuple[EffectModifier, ...] = ()
    search_topics: tuple[SearchTopic, ...] = Field(min_length=1)
    extensions: Extensions = Field(default_factory=dict)

    @property
    def topics(self) -> tuple[SearchTopic, ...]:
        return self.search_topics

    @property
    def criteria(self) -> tuple[Criterion, ...]:
        return tuple(c for t in self.search_topics for c in t.criteria)

    def scopes(self) -> tuple[Scope, ...]:
        """Every scope a decision can be keyed by, in declaration order."""
        lang = self.default_language
        return (
            *(Scope(id=q.id, label=q.label[lang], kind="question") for q in self.questions),
            *(Scope(id=t.id, label=t.label[lang], kind="search_topic") for t in self.search_topics),
        )
