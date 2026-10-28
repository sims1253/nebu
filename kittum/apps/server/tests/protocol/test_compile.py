"""The core compiler on a profile-free review, and the profile mechanism with a toy profile."""

import copy
import json
from typing import Any, Literal

import pytest

from kittum.core import Record
from kittum.protocol import (
    EffectModifier,
    Profile,
    ProtocolError,
    ProtocolIssue,
    ReviewProtocol,
    ScreeningMode,
    compile,
    path_of,
)
from tests.protocol.support import example_source, example_text

NO_PROFILES: dict[str, Profile] = {}


def _compile(source: Any, profiles: dict[str, Profile] | None = None) -> Any:
    return compile(source, profiles=NO_PROFILES if profiles is None else profiles)


def _topic(source: dict[str, Any]) -> dict[str, Any]:
    return source["search_topics"][0]


def _criterion(source: dict[str, Any], criterion_id: str) -> dict[str, Any]:
    return next(c for c in _topic(source)["criteria"] if c["id"] == criterion_id)


def test_generic_review_compiles_without_a_profile() -> None:
    protocol = _compile(example_text()).protocol
    assert protocol.profile is None
    assert [(s.id, s.kind) for s in protocol.scopes()] == [
        ("nrt-vs-control", "question"),
        ("combination-vs-single", "question"),
        ("TRIALS", "search_topic"),
    ]
    control, combination = protocol.questions
    assert [c.id for c in control.comparators] == ["placebo", "no-nrt"]
    assert control.comparators_are_alternatives and not combination.comparators_are_alternatives
    assert {o.category for o in protocol.outcomes} == {"primary", "secondary"}
    (topic,) = protocol.search_topics
    assert topic.mode == ScreeningMode.M1
    assert topic.freshness.max_age_months == 12
    assert topic.scopes[1].label == "Combination NRT versus a single form"
    assert protocol.criteria[0].text == {
        "en": "Adults (18 years or older) who smoke tobacco.",
        "de": "Erwachsene (ab 18 Jahren), die Tabak rauchen.",
    }


def test_hash_is_stable_across_yaml_json_and_key_order() -> None:
    from_yaml = _compile(example_text())
    dumped = json.loads(json.dumps(from_yaml.protocol.model_dump(mode="json")))
    from_json = _compile(json.dumps(dumped))
    from_dump = _compile(dumped)
    reordered = _compile(dict(reversed(list(example_source().items()))))
    assert from_yaml.content_hash == from_json.content_hash == from_dump.content_hash
    assert reordered.content_hash == from_yaml.content_hash
    assert from_dump.protocol == from_yaml.protocol

    edited = example_source()
    edited["title"] = dict(reversed(list(edited["title"].items())))  # language order is not content
    _criterion(edited, "POP")["text"]["en"] += " "  # surrounding whitespace is stripped
    assert _compile(edited).content_hash == from_yaml.content_hash
    _criterion(edited, "POP")["text"]["en"] += " Pregnant smokers included."
    assert _compile(edited).content_hash != from_yaml.content_hash


def _break(edit: Any) -> ProtocolError:
    source = example_source()
    edit(source)
    with pytest.raises(ProtocolError) as caught:
        _compile(source)
    return caught.value


STOPPING = {
    "method": "buscar",
    "target_recall_percent": "95",
    "confidence_percent": "95",
    "minimum_screened_percent": "10",
}

INVALID = {
    "unknown scope in applies_to": (
        lambda s: _criterion(s, "COMP-SINGLE").__setitem__("applies_to", ["nrt-vs-placebo"]),
        "search_topics[0].criteria[3].applies_to[0]",
        "unknown scope 'nrt-vs-placebo'; criterion 'COMP-SINGLE' can apply to its topic or the "
        "topic's scopes (TRIALS, combination-vs-single, nrt-vs-control)",
    ),
    "topic covers an undeclared question": (
        lambda s: _topic(s)["scopes"].append("vaping"),
        "search_topics[0].scopes[2]",
        "unknown scope 'vaping'; a topic covers review questions (nrt-vs-control, "
        "combination-vs-single)",
    ),
    "duplicate criterion ID": (
        lambda s: _criterion(s, "FOLLOWUP").__setitem__("id", "DESIGN"),
        "search_topics[0].criteria[5].id",
        "duplicate criterion ID 'DESIGN', first at search_topics[0].criteria[4].id",
    ),
    "question and topic share an ID": (
        lambda s: _topic(s).__setitem__("id", "nrt-vs-control"),
        "search_topics[0].id",
        "duplicate scope ID 'nrt-vs-control', first at questions[0].id",
    ),
    "no criterion decidable at title and abstract": (
        lambda s: [c.__setitem__("decidable_at", ["full_text"]) for c in _topic(s)["criteria"]],
        "search_topics[0].criteria",
        "topic 'TRIALS' has no criterion decidable at title_abstract",
    ),
    "a scope only decidable at full text": (
        lambda s: [
            c.__setitem__("applies_to", ["nrt-vs-control"])
            for c in _topic(s)["criteria"]
            if "title_abstract" in c["decidable_at"]
        ],
        "search_topics[0].scopes[1]",
        "scope 'combination-vs-single' has no criterion decidable at title_abstract",
    ),
    "text missing in the default language": (
        lambda s: _criterion(s, "DESIGN").__setitem__("text", {"de": "Randomisierte Studie."}),
        "search_topics[0].criteria[4].text",
        "needs text in the default language 'en'",
    ),
    "malformed language code": (
        lambda s: _criterion(s, "DESIGN")["text"].__setitem__("English", "RCT."),
        "search_topics[0].criteria[4].text.English.[key]",
        "String should match pattern",
    ),
    "unknown outcome on a question": (
        lambda s: s["questions"][1]["outcomes"].append("quit-attempts"),
        "questions[1].outcomes[2]",
        "unknown outcome 'quit-attempts'",
    ),
    "unknown mode": (
        lambda s: _topic(s).__setitem__("mode", "M4"),
        "search_topics[0].mode",
        "Input should be 'M1', 'M2' or 'M3', got 'M4'",
    ),
    "misspelt field": (
        lambda s: _criterion(s, "POP").__setitem__("decideable_at", ["full_text"]),
        "search_topics[0].criteria[0].decideable_at",
        "unknown field",
    ),
    "unknown stage": (
        lambda s: _criterion(s, "INT").__setitem__("decidable_at", ["abstract"]),
        "search_topics[0].criteria[1].decidable_at[0]",
        "Input should be 'title_abstract' or 'full_text'",
    ),
    "M2 without a validation pack": (
        lambda s: _topic(s).update(mode="M2", mode_rationale={"en": "Large search."}),
        "search_topics[0].validation_pack",
        "mode M2 needs a validation pack",
    ),
    "M2 without a rationale": (
        lambda s: _topic(s).update(mode="M2", validation_pack="pack-1"),
        "search_topics[0].mode_rationale",
        "a selection not done by two people independently must be justified",
    ),
    "M1 with a stopping plan": (
        lambda s: _topic(s).__setitem__(
            "stopping",
            {
                "method": "buscar",
                "target_recall_percent": "95",
                "confidence_percent": "95",
                "minimum_screened_percent": "10",
            },
        ),
        "search_topics[0].stopping",
        "mode M1 screens every record; only M3 may have a stopping plan",
    ),
    "M3 without a stopping plan": (
        lambda s: _topic(s).update(mode="M3", mode_rationale={"en": "Very large search."}),
        "search_topics[0].stopping",
        "mode M3 needs a stopping plan",
    ),
    "deviation for a source in the plan": (
        lambda s: _topic(s)["source_plan"].__setitem__(
            "deviations", [{"source_id": "medline", "justification": {"en": "No access."}}]
        ),
        "search_topics[0].source_plan.deviations[0].source_id",
        "'medline' is in the plan",
    ),
    "malformed NCT number": (
        lambda s: _topic(s).__setitem__(
            "reference_set", {"validation": [{"kind": "nct", "value": "NCT0430549"}]}
        ),
        "search_topics[0].reference_set.validation[0].value",
        "'NCT0430549' is not a valid nct",
    ),
    "known include in both reference sets": (
        lambda s: _topic(s).__setitem__(
            "reference_set",
            {
                "development": [{"kind": "isrctn", "value": "ISRCTN12345678"}],
                "validation": [{"kind": "isrctn", "value": "ISRCTN12345678"}],
            },
        ),
        "search_topics[0].reference_set.validation[0].value",
        "development and validation sets must not overlap",
    ),
    "question without a search topic": (
        lambda s: (
            _topic(s).__setitem__("scopes", ["nrt-vs-control"]),
            _criterion(s, "COMP-SINGLE").__setitem__("applies_to", ["nrt-vs-control"]),
        ),
        "questions[1]",
        "question 'combination-vs-single' is in no search topic's scopes",
    ),
    "non-positive search age": (
        lambda s: _topic(s)["freshness"].__setitem__("max_age_months", 0),
        "search_topics[0].freshness.max_age_months",
        "Input should be greater than or equal to 1, got 0",
    ),
    "extensions without a profile": (
        lambda s: s["questions"][0].__setitem__("extensions", {"registry": "x"}),
        "questions[0].extensions",
        "extensions need a profile that defines them",
    ),
    "unknown profile": (
        lambda s: s.__setitem__("profile", "nice"),
        "profile",
        "unknown profile 'nice'; registered: none",
    ),
    "M1 without the disagreement census": (
        lambda s: _topic(s)["audit"].__setitem__("census_of_disagreements", False),
        "search_topics[0].audit.census_of_disagreements",
        "mode M1 needs census_of_disagreements: records the AI includes and both humans "
        "exclude go to adjudication",
    ),
    "M2 without the disagreement census": (
        lambda s: _topic(s).update(
            mode="M2",
            mode_rationale={"en": "Large search."},
            validation_pack="pack-1",
            audit={"random_audit_percent": "10", "census_of_disagreements": False},
        ),
        "search_topics[0].audit.census_of_disagreements",
        "mode M2 needs census_of_disagreements: every human-AI disagreement goes into the audit "
        "census",
    ),
    "M2 without a random exclusion audit": (
        lambda s: _topic(s).update(
            mode="M2", mode_rationale={"en": "Large search."}, validation_pack="pack-1"
        ),
        "search_topics[0].audit.random_audit_percent",
        "mode M2 needs a random audit of exclusions above 0 percent",
    ),
    "M3 without a random exclusion audit": (
        lambda s: _topic(s).update(
            mode="M3", mode_rationale={"en": "Very large search."}, stopping=dict(STOPPING)
        ),
        "search_topics[0].audit.random_audit_percent",
        "mode M3 needs a random audit of exclusions above 0 percent",
    ),
    "M3 stopping plan without a target recall": (
        lambda s: _topic(s).update(
            mode="M3",
            mode_rationale={"en": "Very large search."},
            audit={"random_audit_percent": "5"},
            stopping={k: v for k, v in STOPPING.items() if k != "target_recall_percent"},
        ),
        "search_topics[0].stopping.target_recall_percent",
        "Field required",
    ),
    "M3 stopping plan without a confidence": (
        lambda s: _topic(s).update(
            mode="M3",
            mode_rationale={"en": "Very large search."},
            audit={"random_audit_percent": "5"},
            stopping={k: v for k, v in STOPPING.items() if k != "confidence_percent"},
        ),
        "search_topics[0].stopping.confidence_percent",
        "Field required",
    ),
    "scope object in a tuple naming no question": (
        lambda s: _topic(s).__setitem__(
            "scopes",
            (
                "nrt-vs-control",
                "combination-vs-single",
                {"id": "ghost", "label": "Ghost", "kind": "question"},
            ),
        ),
        "search_topics[0].scopes[2]",
        "unknown scope 'ghost'; a topic covers review questions (nrt-vs-control, "
        "combination-vs-single)",
    ),
    "scope object in a tuple with another label": (
        lambda s: _topic(s).__setitem__(
            "scopes",
            (
                {"id": "nrt-vs-control", "label": "All smokers", "kind": "question"},
                "combination-vs-single",
            ),
        ),
        "search_topics[0].scopes[0]",
        "scope 'nrt-vs-control' does not match its question",
    ),
}


@pytest.mark.parametrize("case", INVALID.keys())
def test_invalid_protocols_point_at_the_problem(case: str) -> None:
    edit, path, message = INVALID[case]
    error = _break(edit)
    found = [i for i in error.issues if i.path == path]
    assert found, f"no issue at {path}: {error.issues}"
    assert any(message in i.message for i in found), found


def test_every_problem_is_reported_at_once() -> None:
    def edit(source: dict[str, Any]) -> None:
        _criterion(source, "INT").__setitem__("applies_to", ["x"])
        _topic(source)["mode"] = "M3"
        source["questions"][0]["label"] = {"de": "NRT gegen Kontrolle"}

    issues = {i.path for i in _break(edit).issues}
    assert issues == {
        "questions[0].label",
        "search_topics[0].criteria[1].applies_to[0]",
        "search_topics[0].mode_rationale",
        "search_topics[0].stopping",
        "search_topics[0].audit.random_audit_percent",
    }


def test_yaml_errors_name_the_line() -> None:
    text = example_text().replace("    mode: M1\n", "    mode: M1\n    mode: M2\n", 1)
    with pytest.raises(ProtocolError) as caught:
        _compile(text)
    (issue,) = caught.value.issues
    line = text.splitlines().index("    mode: M2") + 1
    assert issue.message == f"duplicate key 'mode' at line {line}, column 5"


def test_a_dumped_scope_must_match_its_question() -> None:
    tampered = copy.deepcopy(_compile(example_text()).protocol.model_dump(mode="json"))
    tampered["search_topics"][0]["scopes"][0]["label"] = "All smokers"
    with pytest.raises(ProtocolError) as caught:
        _compile(tampered)
    assert [(i.path, i.message) for i in caught.value.issues] == [
        ("search_topics[0].scopes[0]", "scope 'nrt-vs-control' does not match its question")
    ]


# A toy profile: a registration per question, a closed outcome vocabulary, a rule, and defaults.


class RegistryQuestion(Record):
    registry: Literal["prospero", "osf"]
    registration_id: str


def _registered_once(protocol: ReviewProtocol) -> list[ProtocolIssue]:
    seen: set[str] = set()
    issues = []
    for i, q in enumerate(protocol.questions):
        reg = q.extensions["registration_id"]
        if reg in seen:
            issues.append(
                ProtocolIssue(
                    path=path_of("questions", i, "extensions", "registration_id"),
                    message=f"registration {reg!r} is used twice",
                )
            )
        seen.add(reg)
    return issues


TOY = Profile(
    id="toy",
    label={"en": "Toy"},
    extensions={"question": RegistryQuestion},
    vocabularies={
        "outcome_category": {"primary": {"en": "Primary"}, "secondary": {"en": "Secondary"}}
    },
    rules=(_registered_once,),
    labels={"slot.comparator": {"en": "Control"}},
    default_effect_modifiers=(EffectModifier(id="sex", label={"en": "Sex"}),),
)


def _on_toy(source: dict[str, Any]) -> dict[str, Any]:
    source["profile"] = "toy"
    del source["effect_modifiers"]
    for i, q in enumerate(source["questions"]):
        q["extensions"] = {"registry": "prospero", "registration_id": f"CRD42{i}"}
    return source


def _toy_issues(edit: Any) -> list[tuple[str, str]]:
    source = _on_toy(example_source())
    edit(source)
    with pytest.raises(ProtocolError) as caught:
        _compile(source, {"toy": TOY})
    return [(i.path, i.message) for i in caught.value.issues]


def test_a_profile_validates_its_extensions_vocabularies_and_rules() -> None:
    protocol = _compile(_on_toy(example_source()), {"toy": TOY}).protocol
    assert protocol.profile == "toy"
    assert protocol.questions[0].extensions == {"registry": "prospero", "registration_id": "CRD420"}
    assert [m.id for m in protocol.effect_modifiers] == ["sex"]

    assert _toy_issues(lambda s: s["questions"][1]["extensions"].__setitem__("registry", "x")) == [
        ("questions[1].extensions.registry", "Input should be 'prospero' or 'osf', got 'x'")
    ]
    assert _toy_issues(lambda s: s["outcomes"][0].__setitem__("category", "tertiary")) == [
        (
            "outcomes[0].category",
            "'tertiary' is not a outcome_category of profile 'toy' (primary, secondary)",
        )
    ]
    assert _toy_issues(lambda s: s["search_topics"][0].__setitem__("extensions", {"a": 1})) == [
        ("search_topics[0].extensions", "profile 'toy' defines no search_topic extensions")
    ]
    assert _toy_issues(
        lambda s: s["questions"][1]["extensions"].__setitem__("registration_id", "CRD420")
    ) == [("questions[1].extensions.registration_id", "registration 'CRD420' is used twice")]


def test_m3_with_its_stopping_plan_and_audit_compiles() -> None:
    source = example_source()
    _topic(source).update(
        mode="M3",
        mode_rationale={"en": "Very large search."},
        stopping=dict(STOPPING),
        audit={"random_audit_percent": "5"},
    )
    assert _compile(source).protocol.search_topics[0].stopping is not None


def test_scopes_as_a_tuple_of_ids_resolve_like_a_list() -> None:
    source = example_source()
    _topic(source)["scopes"] = tuple(_topic(source)["scopes"])
    assert _compile(source).content_hash == _compile(example_text()).content_hash


def test_question_labels_with_surrounding_whitespace_still_resolve_as_scopes() -> None:
    source = example_source()
    question = source["questions"][1]
    lang = source["default_language"]
    question["label"][lang] = f"  {question['label'][lang]}\n"
    protocol = _compile(source).protocol
    assert protocol.search_topics[0].scopes[1].label == "Combination NRT versus a single form"


PERCENT_EDGES = [
    ("audit", "sentinel_percent", "0", None),
    ("audit", "sentinel_percent", "100", None),
    ("audit", "sentinel_percent", "100.0", None),
    ("audit", "sentinel_percent", "100.9", "Value error, must be in [0, 100], got '100.9'"),
    ("audit", "sentinel_percent", "100.0001", "Value error, must be in [0, 100]"),
    ("audit", "sentinel_percent", "1000", "String should match pattern"),
    ("audit", "sentinel_percent", "-1", "String should match pattern"),
    ("stopping", "target_recall_percent", "0", "Value error, must be in (0, 100], got '0'"),
    ("stopping", "target_recall_percent", "0.1", None),
    ("stopping", "target_recall_percent", "100", None),
    ("stopping", "confidence_percent", "0", "Value error, must be in (0, 100)"),
    ("stopping", "confidence_percent", "99.99", None),
    ("stopping", "confidence_percent", "100", "Value error, must be in (0, 100), got '100'"),
    ("stopping", "minimum_screened_percent", "0", None),
    ("stopping", "minimum_screened_percent", "100.5", "Value error, must be in [0, 100]"),
]


@pytest.mark.parametrize(("part", "field", "value", "error"), PERCENT_EDGES)
def test_percentages_are_exact_decimals_in_range(
    part: str, field: str, value: str, error: str | None
) -> None:
    source = example_source()
    topic = _topic(source)
    topic.update(
        mode="M3",
        mode_rationale={"en": "Very large search."},
        stopping=dict(STOPPING),
        audit={"random_audit_percent": "5"},
    )
    topic[part][field] = value
    if error is None:
        _compile(source)
        return
    with pytest.raises(ProtocolError) as caught:
        _compile(source)
    (issue,) = caught.value.issues
    assert issue.path == f"search_topics[0].{part}.{field}"
    assert error in issue.message
