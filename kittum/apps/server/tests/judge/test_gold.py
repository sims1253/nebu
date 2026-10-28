"""The judge's filter on real model answers for the demo review, against the screeners' decisions.

`fixtures/demo_gold.json` holds the demo's 30 title and abstract items, what both screeners
decided for each (the end-to-end test's plan), and two models' recorded answers. A `not_met` is
correct if it is on the criterion the screeners excluded on. A verified `not_met` on a record they
passed, or on another criterion, is an exclusion they contradict.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from kittum.judge import PROMPT_VERSION, CriterionVerdict, JudgeConfig, ScreeningItem, judge
from kittum.models.testing import RecordedBackend
from kittum.protocol import Criterion

GOLD = json.loads((Path(__file__).parent / "fixtures" / "demo_gold.json").read_text())
PROTOCOL = yaml.safe_load((Path(__file__).parents[4] / GOLD["protocol"]).read_text())
(TOPIC,) = (t for t in PROTOCOL["search_topics"] if t["id"] == GOLD["scope"])
CRITERIA = tuple(Criterion.model_validate(c) for c in TOPIC["criteria"])
CONFIG = JudgeConfig(id="gold", model_id="gold", prompt_version=PROMPT_VERSION)
GLM, QWEN = "glm-5.3-flash", "qwen3.5:4b"


def _record(prefix: str) -> dict[str, Any]:
    (record,) = (r for r in GOLD["records"] if r["title"].startswith(prefix))
    return record


def _title(prefix: str) -> str:
    return _record(prefix)["title"]


TRANSDERMAL = _title("Transdermal nicotine for active ulcerative colitis")
COHORT = _title("Use of nicotine replacement therapy and quit success")
COMBACAL = _title("Community-Based Chronic Disease Care in Rural Lesotho")

# Verified exclusions: (criterion, title). Before the negation and design checks read the
# criterion, the filter verified none of GLM's and only qwen's POP quote "did not smoke tobacco".
VERIFIED = {
    GLM: {("POP", TRANSDERMAL), ("DESIGN", COHORT)},
    QWEN: {("POP", TRANSDERMAL), ("DESIGN", COHORT)},
}
# Correct not_met readings the filter still turns unclear, and why. A quote that names another
# intervention ("received varenicline") does not show that no arm got NRT, so it stays unclear.
STILL_UNCLEAR = {
    GLM: {
        # "This ComBaCaL cohort study": the record also says that "nested trials" will follow,
        # and a record that names the required design anywhere is not read.
        ("DESIGN", COMBACAL, "polarity"),
        ("INT", _title("Electronic Nicotine-Delivery Systems"), "language_unverifiable"),
        ("INT", _title("Varenicline versus placebo for smoking cessation"), "polarity"),
    },
    QWEN: {
        ("POP", _title("Assessing acute nicotine impact"), "no_quote"),
        ("DESIGN", _title("Co-action and changes in alcohol use"), "no_quote"),
        ("DESIGN", _title("Economic Evaluation of Enhanced vs Standard"), "no_quote"),
    },
}

# not_met readings on a record the screeners passed, or on a criterion they did not name.
OTHER = {
    GLM: {
        # The sentence names the design the criterion requires: "In a randomized
        # double-blinded prospective cross-sectional study".
        ("DESIGN", _title("Assessing acute nicotine impact"), "polarity"),
        # One arm got varenicline with an NRT patch.
        ("INT", _title("Economic Evaluation of Enhanced vs Standard"), "language_unverifiable"),
        # The trial behind the study also had a nicotine patch arm.
        ("INT", _title("Genetic Prediction of Smoking Cessation"), "polarity"),
    },
    QWEN: {
        ("POP", _title("Community health worker-led"), "no_quote"),
        ("DESIGN", _title("Assessing acute nicotine impact"), "no_quote"),
        ("INT", _title("Smoking Cessation Pharmacotherapy Efficacy"), "language_unverifiable"),
    },
}


def _readings(
    record: dict[str, Any], model: str, title: str | None = None, text: str | None = None
) -> tuple[CriterionVerdict, ...]:
    item = ScreeningItem(
        item_ref="gold",
        stage="title_abstract",
        title=record["title"] if title is None else title,
        text=record["text"] if text is None else text,
    )
    backend = RecordedBackend(default=record["answers"][model])
    return judge(item, GOLD["scope"], CRITERIA, CONFIG, backend).criteria


@pytest.mark.parametrize("model", [GLM, QWEN])
def test_verified_exclusions_on_real_answers_are_the_screeners_own(model: str) -> None:
    verified, unclear, other = set(), set(), []
    for record in GOLD["records"]:
        for c in _readings(record, model):
            if (c.downgraded_from or c.status) != "not_met":
                continue
            if c.status == "not_met":
                assert c.quote_verified
                # None on a record the screeners passed, none on another criterion than theirs.
                assert record["reviewers"] == c.criterion_id, (record["title"], c)
                verified.add((c.criterion_id, record["title"]))
            elif record["reviewers"] == c.criterion_id:
                unclear.add((c.criterion_id, record["title"], c.failed_check))
            else:
                other.append((c.criterion_id, record["title"], c.failed_check))

    assert verified == VERIFIED[model]
    assert unclear == STILL_UNCLEAR[model]
    # Recall of correct not_met readings: 2 of 5 for GLM (was 0), 2 of 5 for qwen (was 1).
    assert (len(verified), len(verified) + len(unclear)) == {GLM: (2, 5), QWEN: (2, 5)}[model]
    # Readings the screeners contradict, or on a criterion they did not name, all stay unclear.
    assert set(other) == OTHER[model]


# The same real records, edited so that the quote, still verbatim, no longer shows what the model
# claims: (title, text to replace, replacement, the check that must now fail).
EDITS = [
    # The negation now says who was left out, not who took part.
    (TRANSDERMAL, "We enrolled 64 adults", "We excluded 64 adults", "negation"),
    # The sentence also enrolled smokers: the quote is about one cohort only.
    (
        TRANSDERMAL,
        "who did not smoke tobacco.",
        "who did not smoke tobacco and 58 adults who smoke tobacco.",
        "negation",
    ),
    # The negation is about smoking history, not about smoking now.
    (
        TRANSDERMAL,
        "who did not smoke tobacco.",
        "who did not smoke tobacco before age 18.",
        "negation",
    ),
    # The cohort is part of a randomised trial: the sentence names the required design too.
    (COHORT, "cohort study", "cohort study in a randomised trial", "polarity"),
    # Another sentence of the record says it is randomised.
    (COHORT, "We followed", "Within a randomised trial, we followed", "polarity"),
    # A negation outside the quote reverses it.
    (COHORT, ": a prospective", ": not a prospective", "negation"),
]


@pytest.mark.parametrize(
    ("model", "criterion", "title", "old", "new", "check"),
    [
        pytest.param(model, criterion, title, old, new, check, id=f"{model}-{new}")
        for model, exclusions in VERIFIED.items()
        for criterion, title in sorted(exclusions)
        for edited, old, new, check in EDITS
        if edited == title
    ],
)
def test_an_edit_that_changes_what_the_quote_shows_turns_it_unclear(
    model: str, criterion: str, title: str, old: str, new: str, check: str
) -> None:
    record = _record(title)
    assert (old in record["title"]) != (old in record["text"])
    (c,) = (
        c
        for c in _readings(
            record,
            model,
            title=record["title"].replace(old, new, 1),
            text=record["text"].replace(old, new, 1),
        )
        if c.criterion_id == criterion
    )
    assert c.quote_verified  # the quote is still in the record, but no longer excludes
    assert (c.status, c.downgraded_from, c.failed_check) == ("unclear", "not_met", check)
