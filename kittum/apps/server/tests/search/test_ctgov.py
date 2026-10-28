from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kittum.core import sha256_hex
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.ledger.testing import InMemoryLedger
from kittum.search import (
    CtgovSource,
    SearchPlan,
    get_execution,
    run,
)
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).parent / "fixtures"


def test_ctgov_paginated_search_returns_every_page(
    pg: Tx, project_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    strategy = "lumacaftor AND AREA[Phase]PHASE2"

    page1_bytes = (FIXTURES / "ctgov_page1.json").read_bytes()
    page2_bytes = (FIXTURES / "ctgov_page2.json").read_bytes()

    # Recorded with tests/search/record_fixtures.py: nine trials in two pages.
    token = "ZVNj7o2Elu8o3lpzTd6s47mtmpOQJJxtY_el3_ASkA"
    url_p1 = (
        "https://clinicaltrials.gov/api/v2/studies?"
        "query.term=lumacaftor%20AND%20AREA%5BPhase%5DPHASE2&pageSize=5&countTotal=true"
    )
    url_p2 = f"{url_p1}&pageToken={token}"

    transport = FakeTransport(
        {
            url_p1: page1_bytes,
            url_p2: page2_bytes,
        }
    )
    gateway = PassThroughGateway(transport)
    artifacts = MemoryStore()
    source = CtgovSource(gateway, artifacts, page_size=5)

    plan = SearchPlan(
        topic_id="cf-lumacaftor",
        source="ctgov",
        platform="ClinicalTrials.gov",
        strategy=strategy,
    )
    ledger = InMemoryLedger()

    execution = run(
        pg,
        project_id,
        plan,
        source,
        ledger=ledger,
        actor_id=actor_id,
        artifacts=artifacts,
    )

    # 1. The reported count is CT.gov's totalCount; all nine came back across both pages
    assert execution.reported_count == 9
    assert execution.retrieved_count == 9
    assert [r.ordinal for r in execution.records] == list(range(9))

    # 2. Both pages were fetched
    assert len(transport.sent) == 2
    assert transport.sent[0].url == url_p1
    assert transport.sent[1].url == url_p2

    # 3. Both page hashes stored
    h1 = sha256_hex(page1_bytes)
    h2 = sha256_hex(page2_bytes)
    assert execution.raw_artifact_hashes == (h1, h2)
    assert artifacts.get(h1) == page1_bytes
    assert artifacts.get(h2) == page2_bytes
    assert [r.raw_hash for r in execution.records] == [h1] * 5 + [h2] * 4

    # 4. Fields mapped properly on SourceRecord
    first_rec = execution.records[0]
    assert first_rec.source == "ctgov"
    assert first_rec.provider_record_id.startswith("NCT")
    assert first_rec.title != ""
    assert first_rec.abstract is not None
    assert len(first_rec.registry_ids) > 0
    assert first_rec.provider_record_id in first_rec.registry_ids

    # Check phase and status in publication_types
    assert any("Status:" in pt for pt in first_rec.publication_types)
    assert any("Phase" in pt for pt in first_rec.publication_types)

    # 5. Persisted in database and re-loaded
    loaded = get_execution(pg, project_id, execution.id)
    assert loaded is not None
    assert len(loaded.records) == 9
    assert loaded.records[0].provider_record_id == first_rec.provider_record_id

    # 6. Ledger event emitted
    events = ledger.events(pg, project_id)
    assert len(events) == 1
    assert events[0].type == "search.executed"
    assert events[0].actor_id == actor_id
    assert events[0].payload["reported_count"] == 9
    assert events[0].payload["retrieved_count"] == 9
    assert events[0].payload["raw_artifact_hashes"] == [h1, h2]
