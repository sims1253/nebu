"""How the assistant reads a model's answer before anything is stored."""

import json
from typing import Any

from kittum import assist
from kittum.app import recorded_backend
from kittum.core import new_id, sha256_hex
from kittum.models import Completion, CompletionRequest, request_key
from kittum.storage import MemoryStore
from tests.assist.support import DESCRIPTION


class Scripted:
    """Answers each request with the next text."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        text = self.texts.pop(0)
        return Completion(
            text=text,
            model_identity="scripted",
            request_hash=request_key(request),
            raw_response_hash=sha256_hex(text.encode()),
        )


def test_a_language_given_twice_goes_back_to_the_model_with_its_place() -> None:
    # GLM's draft that compiled, once with a second English title the protocol cannot hold.
    store = MemoryStore()
    recipe_id, recorded = recorded_backend(store)
    final = assist.draft(recorded, recipe_id, DESCRIPTION).model.attempts[-1].raw_response_hash
    answer = store.get(final or "").decode()
    doubled: dict[str, Any] = json.loads(answer)
    doubled["title"] = [*doubled["title"], {"key": "en", "value": "A second English title"}]

    drafted = assist.draft(Scripted(json.dumps(doubled), answer), recipe_id, DESCRIPTION)

    first, second = drafted.model.attempts
    assert [(i.path, i.message) for i in first.issues] == [
        ("title[1].key", "'en' is given twice; it is already at title[0]")
    ]
    assert not second.issues and drafted.document is not None
    assert drafted.document["title"] == {"en": json.loads(answer)["title"][0]["value"]}


def test_a_draft_without_its_registry_strategy_goes_back_to_the_model() -> None:
    # GLM's draft that compiled, with the ClinicalTrials.gov strategy left out. A hand-written
    # protocol may leave it out; a draft may not, since the prompt asks for both.
    store = MemoryStore()
    recipe_id, recorded = recorded_backend(store)
    final = assist.draft(recorded, recipe_id, DESCRIPTION).model.attempts[-1].raw_response_hash
    answer = store.get(final or "").decode()
    missing: dict[str, Any] = json.loads(answer)
    sources = missing["search_topics"][0]["source_plan"]["sources"]
    assert [s["id"] for s in sources] == ["medline", "clinicaltrials_gov"]
    sources[1]["strategy"] = None

    model = Scripted(json.dumps(missing), answer)
    drafted = assist.draft(model, recipe_id, DESCRIPTION)

    first, second = drafted.model.attempts
    path = "search_topics[0].source_plan.sources[1].strategy"
    assert [(i.path, i.message) for i in first.issues] == [
        (path, "write the ClinicalTrials.gov search strategy for this source")
    ]
    assert path in model.requests[1].messages[-1].content
    assert not second.issues and drafted.document is not None
    registry = drafted.document["search_topics"][0]["source_plan"]["sources"][1]
    assert (
        registry["strategy"]
        == json.loads(answer)["search_topics"][0]["source_plan"]["sources"][1]["strategy"]
    )


def test_a_europe_pmc_strategy_is_improved_in_europe_pmcs_syntax() -> None:
    """The request for a Europe PMC strategy tells the model Europe PMC's syntax; the request for
    the PubMed strategy stays as it was recorded, so GLM's answers to it still replay."""
    store = MemoryStore()
    recipe_id, recorded = recorded_backend(store)
    document = assist.draft(recorded, recipe_id, DESCRIPTION).document
    assert document is not None
    sources = document["search_topics"][0]["source_plan"]["sources"]
    sources.append(
        {
            "id": "europepmc",
            "kind": "bibliographic",
            "name": "Europe PMC",
            "platform": "Europe PMC",
            "strategy": 'TITLE_ABS:"nicotine replacement" AND TITLE_ABS:"smoking cessation"',
        }
    )
    topic = document["search_topics"][0]["id"]
    part = f"search_topics[{topic}].source_plan.sources[europepmc].strategy"
    answer = {
        "changes": [
            {
                "find": 'TITLE_ABS:"nicotine replacement"',
                "replace": '(TITLE_ABS:"nicotine replacement" OR TITLE_ABS:NRT)',
                "rationale": "Adds the abbreviation.",
            }
        ]
    }
    # The PubMed strategy has no such passage: the model is asked again, up to its repairs.
    model = Scripted(*[json.dumps(answer)] * (2 + assist.REPAIRS))

    improved = assist.improve(model, recipe_id, new_id(), document, part)
    assist.improve(
        model,
        recipe_id,
        new_id(),
        document,
        f"search_topics[{topic}].source_plan.sources[medline].strategy",
    )

    asked = [r.messages[-1].content for r in model.requests]
    assert "written for Europe PMC's search" in asked[0]
    assert "written for Europe PMC's search" not in asked[1]
    assert improved.suggestion is not None
    assert [c.after for c in improved.suggestion.changes] == [
        '(TITLE_ABS:"nicotine replacement" OR TITLE_ABS:NRT)'
    ]
