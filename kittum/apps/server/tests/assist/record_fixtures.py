"""Record the model answers and hit counts the assistant's tests and journey replay.

Run once, by hand, with a hosted or local model configured as a deployment configures one:

    KITTUM_RECIPES_FILE=... KITTUM_LLM_ENDPOINTS=<recipe>=<url> KITTUM_LLM_API_KEYS=<recipe>=<VAR> \\
        uv run python -m tests.assist.record_fixtures <recipe>

With `--replay` it answers from the recorded answers instead and records only the counts.
It drafts the protocol from `DESCRIPTION`, improves the example's PubMed strategy and the draft's,
and counts every strategy the tests count on PubMed and ClinicalTrials.gov. The answers go to
`kittum/app/demo_data/assist/answers.json`, keyed by request (no key, no address); the counts to
`fixtures/counts`, which the e2e stack's recordings point at. This is not a test.
"""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from kittum import assist
from kittum.app import recorded_backend
from kittum.core import new_id
from kittum.gateway.testing import PassThroughGateway
from kittum.models import CompletionRequest, ModelRegistry, backends_from_endpoints, request_key
from kittum.protocol import compile, strategy_dialect
from kittum.search import count_hits
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.assist.support import (
    ANSWERS,
    CONTACT_EMAIL,
    COUNTS,
    DESCRIPTION,
    EXAMPLE,
    PUBMED,
    STACK,
    accepted_subset,
    drafted_pubmed,
)
from tests.enrich.record_fixtures import RecordingTransport


class Recorder:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.answers: dict[str, str] = {}
        self.identity = ""

    def complete(self, request: CompletionRequest) -> Any:
        completion = self._inner.complete(request)
        self.answers[request_key(request)] = completion.text
        self.identity = completion.model_identity
        return completion


def _strategies(document: dict[str, Any]) -> list[tuple[str, str]]:
    version = compile(document)
    return [
        (dialect, s.strategy)
        for t in version.protocol.search_topics
        for s in t.source_plan.sources
        if s.strategy and (dialect := strategy_dialect(s))
    ]


def main(recipe_id: str, *, replay: bool = False) -> None:
    settings = Settings.from_env()
    store = MemoryStore()
    if replay:
        recipe_id, inner = recorded_backend(store)
    else:
        registry = ModelRegistry.load(local=settings.recipes_file)
        inner = backends_from_endpoints(settings.llm_endpoints, registry, store)[recipe_id]
    backend = Recorder(inner)

    drafted = assist.draft(backend, recipe_id, DESCRIPTION)
    assert drafted.document, drafted.issues
    example = compile(yaml.safe_load(EXAMPLE.read_text())).protocol.model_dump(mode="json")
    strategies = _strategies(example) + _strategies(drafted.document)
    for base, part in ((example, PUBMED), (drafted.document, drafted_pubmed(drafted.document))):
        improved = assist.improve(backend, recipe_id, new_id(), base, part)
        assert improved.suggestion and len(improved.suggestion.changes) > 1, improved.issues
        changes = improved.suggestion.changes
        subset = [c for c in changes if c.id in accepted_subset([c.id for c in changes])]
        for chosen in (changes, subset):
            strategies.append(
                ("pubmed", assist.value_at(assist.apply(base, "strategy", part, chosen), part))
            )
        print(f"{part}: {[c.rationale for c in changes]}")

    print(f"draft: {len(drafted.model.attempts)} attempts")
    if not replay:
        _write_answers(recipe_id, backend)
    _record_counts(strategies, store)


def _write_answers(recipe_id: str, backend: Recorder) -> None:
    ANSWERS.write_text(
        json.dumps(
            {
                "recipe_id": recipe_id,
                "model_identity": backend.identity,
                "recorded": datetime.now(UTC).date().isoformat(),
                "description": DESCRIPTION,
                "answers": backend.answers,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n"
    )


def _record_counts(strategies: list[tuple[str, str]], store: MemoryStore) -> None:
    COUNTS.mkdir(parents=True, exist_ok=True)
    transport = RecordingTransport(COUNTS)
    gateway = PassThroughGateway(transport)
    for dialect, strategy in dict.fromkeys(strategies):
        time.sleep(1)  # PubMed allows three requests a second without a key
        found = count_hits(gateway, store, dialect, strategy, contact_email=CONTACT_EMAIL)
        print(f"{dialect} {found.count:>7}  {strategy[:90]}")
    transport.write_manifest()
    _point_stack_at()


def _point_stack_at() -> None:
    """The e2e stack answers from one recordings directory; its manifest names these files."""
    manifest = STACK / "manifest.json"
    entries = json.loads(manifest.read_text())
    for url, name in json.loads((COUNTS / "manifest.json").read_text()).items():
        entries[url] = {"file": str(Path("..", "..", "..", "assist", "fixtures", "counts", name))}
    manifest.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], replay="--replay" in sys.argv)
