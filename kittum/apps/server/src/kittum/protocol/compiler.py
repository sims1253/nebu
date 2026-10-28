"""Compile a protocol source into a hashed, cross-checked `ProtocolVersion`.

Compilation runs in passes, and each reports every problem it finds, not just the first:

1. YAML or JSON parsing, rejecting duplicate keys.
2. Schema validation of every core field.
3. Core cross-checks that a schema cannot express: unique IDs, scopes that exist, text in the
   default language, criteria a reviewer can decide at title and abstract, and the screening mode
   rules.
4. The profile, if the protocol names one: its extension schemas, its vocabularies, then its rules.

Each problem carries the path into the source (`search_topics[1].criteria[0].applies_to[2]`), so an
author can find it.
"""

import re
from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any

import yaml
import yaml.constructor
import yaml.resolver
from pydantic import BaseModel, ValidationError

from kittum.core import KittumError, Record, Sha256, content_hash
from kittum.protocol.model import ReviewProtocol, ScreeningMode, SearchTopic
from kittum.protocol.profile import ExtensionPoint, Profile, registered_profiles

HASH_DOMAIN = "kittum.protocol.v1"

_IDENTIFIER_PATTERNS = {
    "doi": re.compile(r"^10\.\d{4,9}/\S+$"),
    "pmid": re.compile(r"^[1-9]\d{0,8}$"),
    "nct": re.compile(r"^NCT\d{8}$"),
    "isrctn": re.compile(r"^ISRCTN\d{8}$"),
    "eudract": re.compile(r"^\d{4}-\d{6}-\d{2}$"),
    "euct": re.compile(r"^\d{4}-\d{6}-\d{2}-\d{2}$"),
}

_LOCALIZED_KEYS = frozenset(
    {"title", "label", "population", "intervention", "text", "name", "definition", "rationale"}
    | {"justification", "mode_rationale"}
)


class ProtocolIssue(Record):
    path: str
    message: str


class ProtocolError(KittumError):
    """A protocol source that does not compile. `issues` lists every problem found."""

    def __init__(self, issues: list[ProtocolIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.path or '<root>'}: {i.message}" for i in issues))


class ProtocolVersion(Record):
    protocol: ReviewProtocol
    content_hash: Sha256


def protocol_hash(protocol: ReviewProtocol) -> str:
    return content_hash(protocol, domain=HASH_DOMAIN)


def compile(
    source: Mapping[str, Any] | str, *, profiles: Mapping[str, Profile] | None = None
) -> ProtocolVersion:
    """Compile a mapping, or YAML or JSON text, into a `ProtocolVersion`.

    `profiles` defaults to the registered profiles. Raises `ProtocolError` with every problem.
    """
    if isinstance(source, str):
        source = _load(source)
    if not isinstance(source, Mapping):
        raise ProtocolError([ProtocolIssue(path="", message="a protocol must be a mapping")])
    profiles = registered_profiles() if profiles is None else profiles
    raw = dict(source)
    issues: list[ProtocolIssue] = []

    profile: Profile | None = None
    profile_id = raw.get("profile")
    if isinstance(profile_id, str):
        profile = profiles.get(profile_id)
        if profile is None:
            known = ", ".join(sorted(profiles)) or "none"
            issues.append(
                ProtocolIssue(
                    path="profile",
                    message=f"unknown profile {profile_id!r}; registered: {known}",
                )
            )
        elif "effect_modifiers" not in raw and profile.default_effect_modifiers:
            raw["effect_modifiers"] = [
                m.model_dump(mode="json") for m in profile.default_effect_modifiers
            ]

    issues += _resolve_topic_scopes(raw)
    try:
        protocol = ReviewProtocol.model_validate(raw)
    except ValidationError as e:
        issues += _issues_from_pydantic(e)
        raise ProtocolError(issues) from None
    issues += _cross_check(protocol)
    if profile_id is not None and profile is None:
        raise ProtocolError(issues)

    protocol, extension_issues = _validate_extensions(protocol, profile)
    issues += extension_issues
    if profile is not None:
        issues += _check_vocabularies(protocol, profile)
        if not extension_issues:  # rules may rely on valid extensions, nothing else
            for rule in profile.rules:
                issues += rule(protocol)
    if issues:
        raise ProtocolError(issues)
    return ProtocolVersion(protocol=protocol, content_hash=protocol_hash(protocol))


# Parsing


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,  # ty: ignore[invalid-argument-type]
)


def _load(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown position"
        problem = e.problem or e.context or "invalid YAML"
        raise ProtocolError([ProtocolIssue(path="", message=f"{problem} at {where}")]) from None
    except yaml.YAMLError as e:
        raise ProtocolError([ProtocolIssue(path="", message=str(e))]) from None


# Paths


def path_of(*parts: str | int) -> str:
    """`path_of("search_topics", 0, "mode")` is "search_topics[0].mode"."""
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def _issues_from_pydantic(
    error: ValidationError, prefix: tuple[str | int, ...] = ()
) -> list[ProtocolIssue]:
    issues = []
    for err in error.errors():
        loc = [p for p in err["loc"] if not (isinstance(p, str) and p.startswith("function-"))]
        message = err["msg"]
        if err["type"] not in {"missing", "extra_forbidden"} and "input" in err:
            shown = repr(err["input"])
            message += f", got {shown if len(shown) <= 60 else shown[:57] + '...'}"
        if err["type"] == "extra_forbidden":
            message = "unknown field"
        issues.append(ProtocolIssue(path=path_of(*prefix, *loc), message=message))
    return issues


# Scope resolution


def _resolve_topic_scopes(raw: dict[str, Any]) -> list[ProtocolIssue]:
    """Let authors name a topic's scopes by question ID. Expands each ID into the full `Scope`.

    A full scope object (as a dumped protocol contains) is checked against the question instead,
    so `compile(version.protocol.model_dump())` round-trips.
    """
    issues: list[ProtocolIssue] = []
    lang = raw.get("default_language")
    declared: dict[str, dict[str, str]] = {}
    for question in raw.get("questions") or []:
        if isinstance(question, Mapping) and isinstance(question.get("id"), str):
            label = question.get("label")
            text = label.get(lang) if isinstance(label, Mapping) else None
            declared[question["id"]] = {
                "id": question["id"],
                "label": text if isinstance(text, str) else "",
                "kind": "question",
            }
    topics = raw.get("search_topics")
    if not isinstance(topics, list):
        return issues
    new_topics: list[Any] = []
    for ti, topic in enumerate(topics):
        if isinstance(topic, Mapping) and isinstance(topic.get("scopes"), list):
            topic = dict(topic)
            resolved: list[Any] = []
            for si, scope in enumerate(topic["scopes"]):
                here = path_of("search_topics", ti, "scopes", si)
                scope_id = scope.get("id") if isinstance(scope, Mapping) else scope
                if not isinstance(scope_id, str):
                    resolved.append(scope)
                    continue
                if scope_id not in declared:
                    known = ", ".join(declared) or "none"
                    issues.append(
                        ProtocolIssue(
                            path=here,
                            message=f"unknown scope {scope_id!r}; a topic covers review "
                            f"questions ({known})",
                        )
                    )
                    continue
                if isinstance(scope, Mapping) and dict(scope) != declared[scope_id]:
                    issues.append(
                        ProtocolIssue(
                            path=here, message=f"scope {scope_id!r} does not match its question"
                        )
                    )
                resolved.append(declared[scope_id])
            topic["scopes"] = resolved
        new_topics.append(topic)
    raw["search_topics"] = new_topics
    return issues


# Core cross-checks


def _duplicates(ids: list[tuple[str, str]], what: str) -> list[ProtocolIssue]:
    """`ids` holds (path, id) pairs. Reports every repeat after the first."""
    counts = Counter(i for _, i in ids)
    first: dict[str, str] = {}
    issues: list[ProtocolIssue] = []
    for path, i in ids:
        if counts[i] > 1:
            if i in first:
                issues.append(
                    ProtocolIssue(path=path, message=f"duplicate {what} {i!r}, first at {first[i]}")
                )
            else:
                first[i] = path
    return issues


def _localized(node: Any, path: tuple[str | int, ...]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every localized text in a dumped protocol, outside extensions."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "extensions":
                continue
            if key in _LOCALIZED_KEYS and isinstance(value, dict):
                yield path_of(*path, key), value
            else:
                yield from _localized(value, (*path, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _localized(value, (*path, i))


def _cross_check(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    lang = protocol.default_language
    for path, text in _localized(protocol.model_dump(mode="json"), ()):
        if lang not in text:
            issues.append(
                ProtocolIssue(path=path, message=f"needs text in the default language {lang!r}")
            )

    outcome_ids = {o.id for o in protocol.outcomes}
    issues += _duplicates(
        [(path_of("outcomes", i, "id"), o.id) for i, o in enumerate(protocol.outcomes)],
        "outcome ID",
    )
    issues += _duplicates(
        [
            (path_of("effect_modifiers", i, "id"), m.id)
            for i, m in enumerate(protocol.effect_modifiers)
        ],
        "effect modifier ID",
    )
    for qi, question in enumerate(protocol.questions):
        issues += _duplicates(
            [
                (path_of("questions", qi, "comparators", ci, "id"), c.id)
                for ci, c in enumerate(question.comparators)
            ],
            "comparator ID",
        )
        for oi, outcome in enumerate(question.outcomes):
            if outcome not in outcome_ids:
                issues.append(
                    ProtocolIssue(
                        path=path_of("questions", qi, "outcomes", oi),
                        message=f"unknown outcome {outcome!r}",
                    )
                )

    scope_ids = [(path_of("questions", i, "id"), q.id) for i, q in enumerate(protocol.questions)]
    scope_ids += [
        (path_of("search_topics", i, "id"), t.id) for i, t in enumerate(protocol.search_topics)
    ]
    issues += _duplicates(scope_ids, "scope ID")
    criterion_ids: list[tuple[str, str]] = []
    covered: set[str] = set()
    for ti, topic in enumerate(protocol.search_topics):
        here = ("search_topics", ti)
        covered |= {s.id for s in topic.scopes}
        criterion_ids += [
            (path_of(*here, "criteria", ci, "id"), c.id) for ci, c in enumerate(topic.criteria)
        ]
        issues += _check_topic(topic, here)
    issues += _duplicates(criterion_ids, "criterion ID")
    for qi, question in enumerate(protocol.questions):
        if question.id not in covered:
            issues.append(
                ProtocolIssue(
                    path=path_of("questions", qi),
                    message=f"question {question.id!r} is in no search topic's scopes, so no "
                    "search would find its evidence",
                )
            )
    return issues


def _check_topic(topic: SearchTopic, here: tuple[str | int, ...]) -> list[ProtocolIssue]:
    issues = _duplicates(
        [(path_of(*here, "scopes", si), s.id) for si, s in enumerate(topic.scopes)], "scope"
    )
    for si, scope in enumerate(topic.scopes):
        if scope.kind != "question":
            issues.append(
                ProtocolIssue(
                    path=path_of(*here, "scopes", si),
                    message="a topic's scopes are review questions",
                )
            )
    valid_targets = {s.id for s in topic.scopes} | {topic.id}
    screenable: set[str] = set()
    for ci, criterion in enumerate(topic.criteria):
        at = (*here, "criteria", ci)
        if len(set(criterion.decidable_at)) != len(criterion.decidable_at):
            issues.append(
                ProtocolIssue(path=path_of(*at, "decidable_at"), message="repeated stage")
            )
        for ri, target in enumerate(criterion.applies_to):
            if target not in valid_targets:
                known = ", ".join(sorted(valid_targets))
                issues.append(
                    ProtocolIssue(
                        path=path_of(*at, "applies_to", ri),
                        message=f"unknown scope {target!r}; criterion {criterion.id!r} can apply "
                        f"to its topic or the topic's scopes ({known})",
                    )
                )
        if "title_abstract" in criterion.decidable_at:
            targets = set(criterion.applies_to)
            screenable |= {s.id for s in topic.scopes} if topic.id in targets else targets
    if not screenable:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "criteria"),
                message=f"topic {topic.id!r} has no criterion decidable at title_abstract",
            )
        )
    else:
        for si, scope in enumerate(topic.scopes):
            if scope.id not in screenable:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*here, "scopes", si),
                        message=f"scope {scope.id!r} has no criterion decidable at title_abstract",
                    )
                )
    issues += _check_mode(topic, here)
    issues += _check_sources(topic, here)
    issues += _check_reference_set(topic, here)
    return issues


def _check_mode(topic: SearchTopic, here: tuple[str | int, ...]) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    if topic.mode != ScreeningMode.M1 and topic.mode_rationale is None:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "mode_rationale"),
                message=f"mode {topic.mode} needs a rationale: a selection not done by two "
                "people independently must be justified",
            )
        )
    if topic.mode == ScreeningMode.M2 and topic.validation_pack is None:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "validation_pack"),
                message="mode M2 needs a validation pack that shows the AI's performance",
            )
        )
    if topic.mode == ScreeningMode.M3 and topic.stopping is None:
        issues.append(
            ProtocolIssue(path=path_of(*here, "stopping"), message="mode M3 needs a stopping plan")
        )
    if topic.mode != ScreeningMode.M3 and topic.stopping is not None:
        issues.append(
            ProtocolIssue(
                path=path_of(*here, "stopping"),
                message=f"mode {topic.mode} screens every record; only M3 may have a stopping plan",
            )
        )
    return issues


def _check_sources(topic: SearchTopic, here: tuple[str | int, ...]) -> list[ProtocolIssue]:
    plan = topic.source_plan
    at = (*here, "source_plan")
    issues = _duplicates(
        [(path_of(*at, "sources", si, "id"), s.id) for si, s in enumerate(plan.sources)],
        "source ID",
    )
    present = {s.id for s in plan.sources}
    for di, deviation in enumerate(plan.deviations):
        if deviation.source_id in present:
            issues.append(
                ProtocolIssue(
                    path=path_of(*at, "deviations", di, "source_id"),
                    message=f"{deviation.source_id!r} is in the plan; a deviation is for a source "
                    "that is left out",
                )
            )
    return issues


def _check_reference_set(topic: SearchTopic, here: tuple[str | int, ...]) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    at = (*here, "reference_set")
    seen: dict[tuple[str, str], str] = {}
    for part in ("development", "validation"):
        for ii, item in enumerate(getattr(topic.reference_set, part)):
            path = path_of(*at, part, ii, "value")
            if not _IDENTIFIER_PATTERNS[item.kind].match(item.value):
                issues.append(
                    ProtocolIssue(path=path, message=f"{item.value!r} is not a valid {item.kind}")
                )
            key = (item.kind, item.value.lower())
            if key in seen:
                issues.append(
                    ProtocolIssue(
                        path=path,
                        message=f"{item.kind} {item.value!r} is already listed at {seen[key]}; "
                        "development and validation sets must not overlap",
                    )
                )
            else:
                seen[key] = path
    return issues


# Profiles


def _extension_points(
    data: dict[str, Any],
) -> Iterator[tuple[ExtensionPoint, tuple[str | int, ...], dict[str, Any]]]:
    yield "protocol", (), data
    for qi, question in enumerate(data["questions"]):
        yield "question", ("questions", qi), question
        for ci, comparator in enumerate(question["comparators"]):
            yield "comparator", ("questions", qi, "comparators", ci), comparator
    for oi, outcome in enumerate(data["outcomes"]):
        yield "outcome", ("outcomes", oi), outcome
    for ti, topic in enumerate(data["search_topics"]):
        yield "search_topic", ("search_topics", ti), topic
        for ci, criterion in enumerate(topic["criteria"]):
            yield "criterion", ("search_topics", ti, "criteria", ci), criterion


def _validate_extensions(
    protocol: ReviewProtocol, profile: Profile | None
) -> tuple[ReviewProtocol, list[ProtocolIssue]]:
    """Validate every `extensions` mapping against the profile and store it normalized."""
    data = protocol.model_dump(mode="json")
    issues: list[ProtocolIssue] = []
    for point, path, node in _extension_points(data):
        extensions = node["extensions"]
        schema: type[BaseModel] | None = profile.extensions.get(point) if profile else None
        if schema is None:
            if extensions:
                issues.append(
                    ProtocolIssue(
                        path=path_of(*path, "extensions"),
                        message="extensions need a profile that defines them"
                        if profile is None
                        else f"profile {profile.id!r} defines no {point} extensions",
                    )
                )
            continue
        try:
            node["extensions"] = schema.model_validate(extensions).model_dump(mode="json")
        except ValidationError as e:
            issues += _issues_from_pydantic(e, (*path, "extensions"))
    if issues:
        return protocol, issues
    return ReviewProtocol.model_validate(data), issues


def _check_vocabularies(protocol: ReviewProtocol, profile: Profile) -> list[ProtocolIssue]:
    issues: list[ProtocolIssue] = []
    checks = [
        (
            "outcome_category",
            [
                (path_of("outcomes", i, "category"), o.category)
                for i, o in enumerate(protocol.outcomes)
            ],
        ),
        (
            "topic_kind",
            [
                (path_of("search_topics", i, "kind"), t.kind)
                for i, t in enumerate(protocol.search_topics)
            ],
        ),
    ]
    for vocabulary, values in checks:
        terms = profile.vocabularies.get(vocabulary)
        if terms is None:
            continue
        for path, value in values:
            if value not in terms:
                issues.append(
                    ProtocolIssue(
                        path=path,
                        message=f"{value!r} is not a {vocabulary} of profile {profile.id!r} "
                        f"({', '.join(terms)})",
                    )
                )
    return issues
