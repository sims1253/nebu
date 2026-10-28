"""Profiles: what a review community adds to the generic protocol.

A profile names its vocabularies (outcome categories, topic kinds), the schemas of its
`extensions`, extra validation rules, and its own labels for core concepts. The protocol selects
one with `profile: <id>`. Core validation calls into the profile; core never imports one. Entry
points register the built-in profiles (`kittum.profiles.load_profiles()`).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pydantic import BaseModel

    from kittum.protocol.compiler import ProtocolIssue
    from kittum.protocol.model import EffectModifier, ReviewProtocol

type ExtensionPoint = Literal[
    "protocol", "question", "comparator", "outcome", "search_topic", "criterion"
]
type Rule = Callable[[ReviewProtocol], list[ProtocolIssue]]
"""Checks a protocol that passed core validation. Returns issues with paths into the source."""

type Labels = Mapping[str, Mapping[str, str]]
"""Concept to language to label, e.g. {"question": {"en": "Subpopulation"}}."""


@dataclass(frozen=True)
class Profile:
    id: str
    label: Mapping[str, str]
    extensions: Mapping[ExtensionPoint, type[BaseModel]] = field(default_factory=dict)
    vocabularies: Mapping[str, Labels] = field(default_factory=dict)
    """Vocabulary name to term to labels. Core reads "outcome_category" and "topic_kind"."""
    rules: tuple[Rule, ...] = ()
    labels: Labels = field(default_factory=dict)
    """Labels for core concepts: "question", "comparator", "outcome", "search_topic", and
    "slot.<pico slot>" for criterion headings."""
    default_effect_modifiers: tuple[EffectModifier, ...] = ()
    """Used when a protocol on this profile lists no effect modifiers."""

    def label_for(self, concept: str, language: str) -> str | None:
        labels = self.labels.get(concept, {})
        return labels.get(language) or labels.get("en")


_REGISTRY: dict[str, Profile] = {}


def register_profile(profile: Profile) -> None:
    """Register a profile. Registering the same profile again is a no-op."""
    existing = _REGISTRY.get(profile.id)
    if existing is not None and existing is not profile:
        raise ValueError(f"a different profile {profile.id!r} is already registered")
    _REGISTRY[profile.id] = profile


def registered_profiles() -> Mapping[str, Profile]:
    return dict(_REGISTRY)


def get_profile(profile_id: str) -> Profile:
    try:
        return _REGISTRY[profile_id]
    except KeyError:
        raise KeyError(f"profile {profile_id!r} is not registered") from None
