from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kittum.core import sha256_hex
from kittum.ledger.testing import InMemoryLedger
from kittum.search import (
    SearchPlan,
    get_execution,
    import_file,
    list_executions,
)
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).parent / "fixtures"


def _seed_project(pg: Tx) -> uuid.UUID:
    row = pg.execute(
        "insert into project (name, policy_version) values ('imports-test', 'v1') returning id"
    ).fetchone()
    assert row
    return row["id"]


def test_cochrane_central_ris_import(pg: Tx) -> None:
    project_id = _seed_project(pg)
    data = (FIXTURES / "central_export.ris").read_bytes()
    expected_hash = sha256_hex(data)

    plan = SearchPlan(
        topic_id="cf-reviews",
        source="central",
        platform="Cochrane Library (Wiley)",
        strategy='[mh "lumacaftor"]',
        limits=("2015:2025",),
    )
    artifacts = MemoryStore()
    ledger = InMemoryLedger()

    execution = import_file(
        pg,
        project_id,
        plan,
        data,
        "central",
        ledger=ledger,
        artifacts=artifacts,
    )

    # Counts
    assert execution.reported_count == 2
    assert execution.retrieved_count == 2
    assert execution.kind == "file_import"
    assert execution.raw_artifact_hashes == (expected_hash,)
    assert artifacts.exists(expected_hash)

    # First record
    rec0 = execution.records[0]
    assert rec0.provider_record_id == "CN-01077759"
    assert "TRAFFIC and TRANSPORT" in (rec0.abstract or "")
    assert rec0.year == 2015
    assert rec0.doi == "10.1056/NEJMoa1409547"
    assert "Wainwright, CE" in rec0.authors
    assert "NCT01807897" in rec0.registry_ids

    # Second record with EudraCT number
    rec1 = execution.records[1]
    assert rec1.provider_record_id == "CN-02264891"
    assert "NCT02544451" in rec1.registry_ids
    assert "2015-001234-56" in rec1.registry_ids
    assert rec1.year == 2021

    # Verify database persistence
    loaded = get_execution(pg, project_id, execution.id)
    assert loaded is not None
    assert len(loaded.records) == 2
    assert loaded.records[0].provider_record_id == "CN-01077759"

    # Verify ledger event
    events = ledger.events(pg, project_id)
    assert len(events) == 1
    assert events[0].type == "search.executed"
    assert events[0].payload["strategy"] == '[mh "lumacaftor"]'
    assert events[0].payload["limits"] == ["2015:2025"]


def test_embase_ris_import(pg: Tx) -> None:
    project_id = _seed_project(pg)
    data = (FIXTURES / "embase_export.ris").read_bytes()

    plan = SearchPlan(
        topic_id="cf-reviews",
        source="embase",
        platform="Embase.com",
        strategy="'lumacaftor'/exp AND 'cystic fibrosis'/exp",
    )
    ledger = InMemoryLedger()

    execution = import_file(
        pg,
        project_id,
        plan,
        data,
        "embase",
        ledger=ledger,
    )

    assert execution.reported_count == 2
    rec0 = execution.records[0]
    assert rec0.provider_record_id == "615024109"
    assert rec0.year == 2017
    assert rec0.doi == "10.1016/j.jcf.2017.01.008"
    assert "NCT01807897" in rec0.registry_ids

    rec1 = execution.records[1]
    assert rec1.provider_record_id == "619550824"
    assert "2014-003667-28" in rec1.registry_ids


def test_ctis_csv_import(pg: Tx) -> None:
    project_id = _seed_project(pg)
    data = (FIXTURES / "ctis_export.csv").read_bytes()

    plan = SearchPlan(
        topic_id="cf-registries",
        source="ctis",
        platform="CTIS",
        strategy="lumacaftor",
    )
    ledger = InMemoryLedger()

    execution = import_file(
        pg,
        project_id,
        plan,
        data,
        "ctis",
        ledger=ledger,
    )

    assert execution.reported_count == 2
    rec0 = execution.records[0]
    assert rec0.provider_record_id == "2023-504892-39-00"
    assert "novel CFTR modulator" in rec0.title
    assert rec0.year == 2023
    assert any("Phase III" in pt for pt in rec0.publication_types)
    assert any("Ongoing" in pt for pt in rec0.publication_types)
    assert "Vertex Pharmaceuticals" in rec0.authors


def test_euctr_csv_import(pg: Tx) -> None:
    project_id = _seed_project(pg)
    data = (FIXTURES / "euctr_export.csv").read_bytes()

    plan = SearchPlan(
        topic_id="cf-registries",
        source="euctr",
        platform="EU-CTR",
        strategy="lumacaftor",
    )
    ledger = InMemoryLedger()

    execution = import_file(
        pg,
        project_id,
        plan,
        data,
        "euctr",
        ledger=ledger,
    )

    assert execution.reported_count == 2
    rec0 = execution.records[0]
    assert rec0.provider_record_id == "2015-001234-56"
    assert "2015-001234-56" in rec0.registry_ids
    assert rec0.year == 2015
    assert "Cystic Fibrosis" in (rec0.abstract or "")
    assert "Completed" in " ".join(rec0.publication_types)


def test_list_executions(pg: Tx) -> None:
    project_id = _seed_project(pg)

    plan1 = SearchPlan(
        topic_id="topic-A",
        source="central",
        platform="Cochrane",
        strategy="query1",
    )
    plan2 = SearchPlan(
        topic_id="topic-B",
        source="central",
        platform="Cochrane",
        strategy="query2",
    )

    data = (FIXTURES / "central_export.ris").read_bytes()
    import_file(pg, project_id, plan1, data, "central")
    import_file(pg, project_id, plan2, data, "central")

    all_execs = list_executions(pg, project_id)
    assert len(all_execs) == 2

    topic_a_execs = list_executions(pg, project_id, topic_id="topic-A")
    assert len(topic_a_execs) == 1
    assert topic_a_execs[0].plan.topic_id == "topic-A"
