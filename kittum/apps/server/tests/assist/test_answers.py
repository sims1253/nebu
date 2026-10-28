"""How the assistant reads a model's answer before anything is stored."""

import json
from typing import Any

from kittum import assist
from kittum.app import recorded_backend
from kittum.core import sha256_hex
from kittum.models import Completion, CompletionRequest, request_key
from kittum.storage import MemoryStore
from tests.assist.support import DESCRIPTION


class Scripted:
    """Answers each request with the next text."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)

    def complete(self, request: CompletionRequest) -> Completion:
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
