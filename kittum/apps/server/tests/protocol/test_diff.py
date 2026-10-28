from kittum.protocol import FieldChange, ModeChange, ScreeningMode, compile, diff
from tests.protocol.support import example_source

CRITERIA = "search_topics[TRIALS].criteria"


def test_diff_reports_a_changed_criterion_and_a_mode_change() -> None:
    before = compile(example_source(), profiles={})
    source = example_source()
    topic = source["search_topics"][0]
    topic["mode"] = "M2"
    topic["mode_rationale"] = {"en": "The update search returned 6,000 records."}
    topic["validation_pack"] = "kittum-validation-pack/2026-10"
    design = next(c for c in topic["criteria"] if c["id"] == "DESIGN")
    design["text"]["en"] = "Randomized controlled trial, including cluster-randomized trials."
    topic["criteria"] = [c for c in topic["criteria"] if c["id"] != "FOLLOWUP"]
    topic["criteria"].append(
        {
            "id": "FOLLOWUP-12",
            "slot": "outcome",
            "text": {"en": "Reports smoking status at least twelve months after the start."},
            "decidable_at": ["full_text"],
            "applies_to": ["TRIALS"],
        }
    )
    after = compile(source, profiles={})

    d = diff(before, after)
    assert d.mode_changes == (
        ModeChange(topic_id="TRIALS", before=ScreeningMode.M1, after=ScreeningMode.M2),
    )
    assert d.changed_criteria == ("DESIGN",)
    assert d.removed_criteria == ("FOLLOWUP",)
    assert d.added_criteria == ("FOLLOWUP-12",)
    assert (
        FieldChange(
            path=f"{CRITERIA}[DESIGN].text.en",
            before="Randomized or quasi-randomized controlled trial.",
            after="Randomized controlled trial, including cluster-randomized trials.",
        )
        in d.changes
    )
    assert FieldChange(path="search_topics[TRIALS].mode", before="M1", after="M2") in d.changes
    removed = next(c for c in d.changes if c.path == f"{CRITERIA}[FOLLOWUP]")
    assert removed.after is None and removed.before["slot"] == "outcome"

    back = diff(after, before)
    assert back.mode_changes == (
        ModeChange(topic_id="TRIALS", before=ScreeningMode.M2, after=ScreeningMode.M1),
    )
    assert (back.added_criteria, back.removed_criteria) == (("FOLLOWUP",), ("FOLLOWUP-12",))


def test_a_new_question_shows_as_an_added_scope() -> None:
    before = compile(example_source(), profiles={})
    source = example_source()
    source["questions"].append(
        {
            "id": "preloading",
            "label": {"en": "NRT before the quit date versus after"},
            "population": {"en": "Adults who smoke tobacco and want to stop."},
            "intervention": {"en": "NRT started before the quit date."},
            "comparators": [{"id": "standard", "text": {"en": "NRT from the quit date"}}],
        }
    )
    topic = source["search_topics"][0]
    topic["scopes"].append("preloading")
    topic["criteria"].append(
        {
            "id": "COMP-PRELOAD",
            "slot": "comparator",
            "text": {"en": "NRT starts before the quit date in one arm only."},
            "decidable_at": ["title_abstract", "full_text"],
            "applies_to": ["preloading"],
        }
    )
    after = compile(source, profiles={})

    d = diff(before, after)
    assert d.added_scopes == ("preloading",)
    assert d.added_criteria == ("COMP-PRELOAD",)
    assert d.changed_criteria == () and d.mode_changes == ()


def test_reordering_criteria_is_one_order_change() -> None:
    before = compile(example_source(), profiles={})
    source = example_source()
    criteria = source["search_topics"][0]["criteria"]
    criteria[0], criteria[1] = criteria[1], criteria[0]
    d = diff(before, compile(source, profiles={}))
    assert [c.path for c in d.changes] == [f"{CRITERIA}.order"]
    assert d.changed_criteria == ()
    assert diff(before, before).is_empty
