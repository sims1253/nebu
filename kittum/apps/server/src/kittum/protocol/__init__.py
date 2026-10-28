"""The review protocol: scopes, criteria, and screening modes (DESIGN.md, "Protocol").

The protocol is versioned JSON with a content hash, and it is the source of truth for every later
decision. Exclusion reasons are criterion IDs.

Owner: WP-03. Shipped now: a first cut of the types. Still to build: the full schema from
DESIGN.md (Teilpopulation, zVT, endpoints, source plan, reference set, audit and stopping plans),
`compile` with validation, `diff`, and criterion rendering for humans and models.
"""

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from kittum.core import Record, Sha256

if TYPE_CHECKING:
    from collections.abc import Mapping


class ScreeningMode(StrEnum):
    M1 = "M1"  # two humans, AI as third screener
    M2 = "M2"  # one human, AI as second screener
    M3 = "M3"  # prioritized dual screening with a certified stop


type PicoSlot = Literal[
    "population", "intervention", "comparator", "outcome", "study_type", "other"
]
type Stage = Literal["title_abstract", "full_text"]


class Criterion(Record):
    id: str
    slot: PicoSlot
    text_de: str
    text_en: str
    decidable_at: tuple[Stage, ...]
    applies_to: tuple[str, ...]  # scope IDs
    positive_examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()


class Scope(Record):
    """One Teilpopulation (or JCA PICO) that decisions are keyed by."""

    id: str
    label: str


class SearchTopic(Record):
    id: str
    kind: Literal["rct_drug", "rct_indirect", "non_randomized", "other"]
    mode: ScreeningMode
    scopes: tuple[Scope, ...]
    criteria: tuple[Criterion, ...]


class ReviewProtocol(Record):
    active_substance: str
    version_label: str
    topics: tuple[SearchTopic, ...]


class ProtocolVersion(Record):
    protocol: ReviewProtocol
    content_hash: Sha256


class ProtocolDiff(Record):
    added_criteria: tuple[str, ...]
    removed_criteria: tuple[str, ...]
    changed_criteria: tuple[str, ...]
    mode_changes: tuple[str, ...]


def compile(source: Mapping[str, Any]) -> ProtocolVersion:
    raise NotImplementedError("WP-03")


def diff(a: ProtocolVersion, b: ProtocolVersion) -> ProtocolDiff:
    raise NotImplementedError("WP-03")


__all__ = [
    "Criterion",
    "PicoSlot",
    "ProtocolDiff",
    "ProtocolVersion",
    "ReviewProtocol",
    "Scope",
    "ScreeningMode",
    "SearchTopic",
    "Stage",
    "compile",
    "diff",
]
