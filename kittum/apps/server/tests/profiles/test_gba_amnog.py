"""The G-BA/AMNOG profile on the real capivasertib example (G-BA procedure D-1110)."""

import json
from typing import TYPE_CHECKING, Any

import pytest

from kittum.ledger import PostgresLedger
from kittum.profiles import GBA_AMNOG, load_profiles
from kittum.profiles.gba_amnog import GbaProtocol, GbaQuestion
from kittum.protocol import (
    PUBLISHED_EVENT,
    ProtocolError,
    ProtocolStateError,
    compile,
    diff,
    get_version,
    publish,
    render_question,
    save_draft,
)
from tests.protocol.support import example_source, example_text, seed_project

if TYPE_CHECKING:
    from kittum.db import Tx

NAME = "capivasertib"
PROFILES = {GBA_AMNOG.id: GBA_AMNOG}


def _compile(source: Any) -> Any:
    return compile(source, profiles=PROFILES)


def _source() -> dict[str, Any]:
    return example_source(NAME)


def _criterion(source: dict[str, Any], criterion_id: str) -> dict[str, Any]:
    return next(
        c for t in source["search_topics"] for c in t["criteria"] if c["id"] == criterion_id
    )


def test_example_compiles_into_the_gba_structure() -> None:
    protocol = _compile(example_text(NAME)).protocol
    dossier = GbaProtocol.model_validate(protocol.extensions)
    assert dossier.dossier.active_substance == "Capivasertib"
    assert dossier.dossier.gba_procedure == "D-1110"
    assert [(a.code, a.questions) for a in dossier.anwendungsgebiete] == [
        ("A", ("a1", "a2", "b1", "b2"))
    ]

    questions = {q.id: q for q in protocol.questions}
    zvt = {qid: [c.id for c in q.comparators] for qid, q in questions.items()}
    assert zvt["a2"] == ["tamoxifen", "palbociclib_nsai"]
    assert "everolimus_exemestan" in zvt["b1"] and "everolimus_exemestan" not in zvt["a1"]
    assert len({tuple(options) for options in zvt.values()}) == 4
    assert all(q.comparators_are_alternatives for q in questions.values())
    individualized = {
        qid
        for qid, q in questions.items()
        if (z := GbaQuestion.model_validate(q.extensions).zvt) and z.nach_aerztlicher_massgabe
    }
    assert individualized == {"b1", "b2"}
    off_label = [c.id for c in questions["b2"].comparators if c.extensions["off_label"]]
    assert off_label == ["ai_gnrh", "fulvestrant"]
    assert [o.category for o in protocol.outcomes][:2] == ["mortality", "morbidity"]
    assert {o.category for o in protocol.outcomes} == set(
        GBA_AMNOG.vocabularies["outcome_category"]
    )

    rct, ic = protocol.search_topics
    assert (rct.kind, rct.mode, ic.kind, ic.mode) == ("rct_drug", "M1", "rct_indirect", "M2")
    assert [s.id for s in rct.scopes] == ["a1", "a2", "b1", "b2"]
    assert all({"de", "en"} <= c.text.keys() for c in protocol.criteria)
    assert [c.id for c in rct.criteria if c.slot == "comparator"] == [
        "RCT-E3-a1",
        "RCT-E3-a2",
        "RCT-E3-b1",
        "RCT-E3-b2",
    ]


def test_example_round_trips_with_a_stable_hash() -> None:
    from_yaml = _compile(example_text(NAME))
    dumped = json.loads(json.dumps(from_yaml.protocol.model_dump(mode="json")))
    assert _compile(dumped).content_hash == from_yaml.content_hash
    assert _compile(json.dumps(dumped)).protocol == from_yaml.protocol
    load_profiles()
    assert compile(example_text(NAME)).content_hash == from_yaml.content_hash


def test_default_effect_modifiers_come_from_the_profile() -> None:
    source = _source()
    del source["effect_modifiers"]
    modifiers = _compile(source).protocol.effect_modifiers
    assert [m.id for m in modifiers] == ["sex", "age", "severity", "centre"]
    assert modifiers[3].label == {"de": "Zentrum oder Land", "en": "Centre or country"}


def _drop_source(source: dict[str, Any], source_id: str) -> None:
    plan = source["search_topics"][0]["source_plan"]
    plan["sources"] = [s for s in plan["sources"] if s["id"] != source_id]


INVALID = {
    "search older than three months": (
        lambda s: s["search_topics"][0]["freshness"].__setitem__("max_age_months", 6),
        "search_topics[0].freshness.max_age_months",
        "Modul 4 allows searches at most 3 months old at submission, got 6",
    ),
    "CENTRAL left out without a deviation": (
        lambda s: _drop_source(s, "central"),
        "search_topics[0].source_plan.sources",
        "Cochrane CENTRAL ('central') is part of the Modul 4 minimum; add it or add a deviation",
    ),
    "Teilpopulation without a zVT": (
        lambda s: s["questions"][1]["extensions"].pop("zvt"),
        "questions[1].extensions.zvt",
        "Teilpopulation 'a2' needs the zVT set by the G-BA",
    ),
    "zVT options as separate comparisons": (
        lambda s: s["questions"][0].__setitem__("comparators_are_alternatives", False),
        "questions[0].comparators_are_alternatives",
        "zVT options are alternatives",
    ),
    "zVT without its source document": (
        lambda s: s["questions"][0]["extensions"]["zvt"].pop("source_document"),
        "questions[0].extensions.zvt.source_document",
        "Field required",
    ),
    "English as the default language": (
        lambda s: s.__setitem__("default_language", "en"),
        "default_language",
        "Modul 4 is written in German; set the default language to 'de', got 'en'",
    ),
    "endpoint category outside AMNOG": (
        lambda s: s["outcomes"][1].__setitem__("category", "efficacy"),
        "outcomes[1].category",
        "'efficacy' is not a outcome_category of profile 'gba_amnog' (mortality, morbidity, "
        "quality_of_life, side_effects)",
    ),
    "search kind outside Modul 4": (
        lambda s: s["search_topics"][1].__setitem__("kind", "network_meta_analysis"),
        "search_topics[1].kind",
        "is not a topic_kind of profile 'gba_amnog'",
    ),
    "Teilpopulation in no Anwendungsgebiet": (
        lambda s: s["extensions"]["anwendungsgebiete"][0].__setitem__(
            "questions", ["a1", "a2", "b1"]
        ),
        "questions[3]",
        "question 'b2' is in no Anwendungsgebiet",
    ),
    "topic reaching into another Anwendungsgebiet": (
        lambda s: (
            s["extensions"]["anwendungsgebiete"][0].__setitem__("questions", ["a1", "b1", "b2"]),
            s["extensions"]["anwendungsgebiete"].append(
                {
                    "code": "B",
                    "text_de": "Männer, (neo-)adjuvant vorbehandelt.",
                    "questions": ["a2"],
                    "search_topics": ["IC"],
                }
            ),
            s["extensions"]["anwendungsgebiete"][0].__setitem__("search_topics", ["RCT"]),
        ),
        "search_topics[0].scopes[1]",
        "'a2' belongs to Anwendungsgebiet 'B', the topic to 'A'",
    ),
    "JCA link to a Teilpopulation": (
        lambda s: s["questions"][2]["extensions"].__setitem__("jca_pico_links", ["a1"]),
        "questions[2].extensions.jca_pico_links[0]",
        "'a1' is not a question of kind jca_pico",
    ),
}


@pytest.mark.parametrize("case", INVALID.keys())
def test_profile_rules_point_at_the_problem(case: str) -> None:
    edit, path, message = INVALID[case]
    source = _source()
    edit(source)
    with pytest.raises(ProtocolError) as caught:
        _compile(source)
    found = [i for i in caught.value.issues if i.path == path]
    assert found, f"no issue at {path}: {caught.value.issues}"
    assert any(message in i.message for i in found), found


def test_a_justified_deviation_replaces_a_minimum_source() -> None:
    source = _source()
    _drop_source(source, "ema_clinical_data")
    source["search_topics"][0]["source_plan"]["deviations"] = [
        {"source_id": "ema_clinical_data", "justification": {"de": "Keine Daten veröffentlicht."}}
    ]
    assert _compile(source).protocol.search_topics[0].source_plan.deviations[0].source_id == (
        "ema_clinical_data"
    )


def test_a_jca_pico_adds_a_scope_for_delta_screening() -> None:
    before = _compile(_source())
    source = _source()
    source["questions"].append(
        {
            "id": "PICO-2",
            "label": {"de": "JCA-PICO 2: endokrin vorbehandelt"},
            "population": {"de": "Erwachsene mit ER+/HER2- fortgeschrittenem Mammakarzinom"},
            "intervention": {"de": "Capivasertib + Fulvestrant"},
            "comparators": [
                {"id": "fulvestrant", "text": {"de": "Fulvestrant"}},
                {"id": "everolimus_exemestan", "text": {"de": "Everolimus + Exemestan"}},
            ],
            "extensions": {"kind": "jca_pico", "source_document": "Konsolidierte PICO (Beispiel)"},
        }
    )
    source["questions"][2]["extensions"]["jca_pico_links"] = ["PICO-2"]
    source["extensions"]["anwendungsgebiete"][0]["questions"].append("PICO-2")
    rct = source["search_topics"][0]
    rct["scopes"].append("PICO-2")
    rct["criteria"].append(
        {
            "id": "RCT-PICO2-C",
            "slot": "comparator",
            "text": {
                "de": "Vergleich mit Everolimus + Exemestan.",
                "en": "Comparison with everolimus plus exemestane.",
            },
            "decidable_at": ["title_abstract", "full_text"],
            "applies_to": ["PICO-2"],
        }
    )
    d = diff(before, _compile(source))
    assert d.added_scopes == ("PICO-2",)
    assert d.added_criteria == ("RCT-PICO2-C",)
    assert d.mode_changes == () and d.changed_criteria == ()


def test_profile_labels_appear_in_the_reviewer_question() -> None:
    protocol = _compile(_source()).protocol
    criterion = next(c for c in protocol.criteria if c.id == "RCT-E3-b1")
    q = render_question(criterion, "de", "full_text", profile=GBA_AMNOG)
    assert q.heading == "RCT-E3-b1 · zVT · Einschluss · Volltext"
    assert GBA_AMNOG.label_for("question", "de") == "Teilpopulation"
    assert GBA_AMNOG.label_for("anwendungsgebiet", "en") == "Therapeutic indication"


@pytest.mark.postgres
def test_example_publishes_into_postgres_with_a_ledger_event(pg: Tx) -> None:
    load_profiles()  # reading a stored version recompiles it with the registered profiles
    project, approver, _ = seed_project(pg)
    ledger = PostgresLedger()
    version = _compile(example_text(NAME))
    draft = save_draft(pg, project, version)
    published = publish(pg, project, draft.id, approver, ledger=ledger)
    assert get_version(pg, project, draft.id) == published
    (event,) = ledger.events(pg, project)
    assert (event.type, event.event_id) == (PUBLISHED_EVENT, published.approved_event_id)
    assert event.payload["content_hash"] == version.content_hash

    source = _source()
    source["version_label"] = "1.1"
    source["search_topics"][0].update(
        mode="M2",
        mode_rationale={"de": "Update-Recherche mit großer Treffermenge."},
        validation_pack="kittum-validation-pack/2026-10",
        audit={"sentinel_percent": "1", "random_audit_percent": "10"},
    )
    v2 = save_draft(pg, project, _compile(source))
    with pytest.raises(ProtocolStateError, match=r"RCT M1→M2"):
        publish(pg, project, v2.id, approver, ledger=ledger)
    publish(pg, project, v2.id, approver, ledger=ledger, reason="Validierungspaket liegt vor.")
    assert ledger.events(pg, project)[-1].payload["mode_changes"] == [
        {"topic_id": "RCT", "before": "M1", "after": "M2"}
    ]
    assert ledger.verify(pg, project).ok


def test_profile_extensions_are_frozen_all_the_way_down() -> None:
    protocol = _compile(example_text(NAME)).protocol
    zvt = protocol.questions[2].extensions["zvt"]
    with pytest.raises(TypeError, match="immutable"):
        zvt["nach_aerztlicher_massgabe"] = False
    with pytest.raises(TypeError, match="immutable"):
        protocol.extensions["anwendungsgebiete"][0]["questions"] = ["a1"]
    assert isinstance(zvt["qualifying_comparator_arms"], tuple)
    assert _compile(protocol.model_dump(mode="json")).protocol == protocol
