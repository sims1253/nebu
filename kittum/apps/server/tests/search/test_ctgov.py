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


def _seed_project(pg: Tx) -> uuid.UUID:
    row = pg.execute(
        "insert into project (name, policy_version) values ('ctgov-test', 'v1') returning id"
    ).fetchone()
    assert row
    return row["id"]


def test_ctgov_paginated_search_returns_every_page(pg: Tx) -> None:
    project_id = _seed_project(pg)
    strategy = "lumacaftor"

    page1_bytes = (FIXTURES / "ctgov_page1.json").read_bytes()
    page2_bytes = (FIXTURES / "ctgov_page2.json").read_bytes()

    token = "ZVNj7o2Elu8o3lpzTd6s47mtmpOQJJxtZPGg2PcW"
    url_p1 = f"https://clinicaltrials.gov/api/v2/studies?query.term={strategy}&pageSize=5"
    url_p2 = f"https://clinicaltrials.gov/api/v2/studies?query.term={strategy}&pageSize=5&pageToken={token}"

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
        artifacts=artifacts,
    )

    # 1. Total 10 records retrieved across the 2 pages
    assert len(execution.records) == 10
    assert execution.retrieved_count == 10

    # 2. Both pages were fetched
    assert len(transport.sent) == 2
    assert transport.sent[0].url == url_p1
    assert transport.sent[1].url == url_p2

    # 3. Both page hashes stored
    h1 = sha256_hex(page1_bytes)
    h2 = sha256_hex(page2_bytes)
    assert h1 in execution.raw_artifact_hashes
    assert h2 in execution.raw_artifact_hashes
    assert artifacts.exists(h1)
    assert artifacts.exists(h2)

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
    assert len(loaded.records) == 10
    assert loaded.records[0].provider_record_id == first_rec.provider_record_id

    # 6. Ledger event emitted
    events = ledger.events(pg, project_id)
    assert len(events) == 1
    assert events[0].type == "search.executed"
    assert events[0].payload["retrieved_count"] == 10
