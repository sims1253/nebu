import json
from typing import TYPE_CHECKING

import pytest
from tests.search.record_fixtures import DUPLICATES_DIR, DUPLICATES_EUROPEPMC

from kittum.gateway import RecordedTransport
from kittum.gateway.testing import FakeTransport, PassThroughGateway, response
from kittum.ledger.testing import InMemoryLedger
from kittum.search import EuropePmcSource, SearchPlan, SourceError, get_execution, run
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx
    from kittum.gateway import HttpRequest, HttpResponse

pytestmark = pytest.mark.postgres

PLAN = SearchPlan(
    topic_id="nrt", source="europepmc", platform="Europe PMC", strategy=DUPLICATES_EUROPEPMC
)


class Spy:
    """Answers from the recordings and keeps what was asked."""

    def __init__(self) -> None:
        self.inner = RecordedTransport(DUPLICATES_DIR)
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        return self.inner.send(request)


def test_europe_pmc_pages_with_its_cursor_until_it_has_every_record(
    pg: Tx, project_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """Two records a page: the second page asks from the first page's cursor, and the search stops
    once it holds the count Europe PMC reported. Synonyms stay off on every page."""
    transport = Spy()
    artifacts = MemoryStore()
    source = EuropePmcSource(PassThroughGateway(transport), artifacts, page_size=2)

    execution = run(
        pg,
        project_id,
        PLAN,
        source,
        ledger=InMemoryLedger(),
        actor_id=actor_id,
        artifacts=artifacts,
    )

    first = json.loads((DUPLICATES_DIR / _first_page(2)).read_bytes())
    assert len(transport.sent) == 2
    assert "cursorMark=%2A" in transport.sent[0].url
    assert f"cursorMark={first['nextCursorMark'].replace('=', '%3D')}" in transport.sent[1].url
    assert all("synonym=FALSE" in r.url for r in transport.sent)
    assert (execution.reported_count, execution.retrieved_count) == (4, 4)
    assert len(execution.raw_artifact_hashes) == 2
    stored = get_execution(pg, project_id, execution.id)
    assert stored is not None
    assert [(r.ordinal, r.pmid, r.pmcid) for r in stored.records] == [
        (0, "38354139", None),
        (1, "38482972", "PMC11831237"),
        (2, "37742229", "PMC10803114"),
        (3, "37474127", "PMC10734386"),
    ]
    # Each record points at the page it came from.
    assert [r.raw_hash for r in stored.records] == [execution.raw_artifact_hashes[0]] * 2 + [
        execution.raw_artifact_hashes[1]
    ] * 2


def test_a_europe_pmc_search_that_returns_fewer_records_than_it_counted_is_refused() -> None:
    """PRISMA counts what the source reported; a search that cannot fetch all of it fails."""
    page = json.loads((DUPLICATES_DIR / _first_page(1000)).read_bytes())
    page["hitCount"] = 5
    page.pop("nextCursorMark", None)
    transport = FakeTransport(sequence=[response(200, json.dumps(page).encode())])
    source = EuropePmcSource(PassThroughGateway(transport), MemoryStore())
    with pytest.raises(SourceError, match="Europe PMC reported 5 records but returned 4"):
        source.run(PLAN)


def _first_page(page_size: int) -> str:
    """The recorded first page of the Europe PMC search, at a page size."""
    manifest = json.loads((DUPLICATES_DIR / "manifest.json").read_text())
    return next(
        entries[0]["file"]
        for url, entries in manifest.items()
        if "europepmc" in url and f"pageSize={page_size}&cursorMark=%2A" in url
    )
