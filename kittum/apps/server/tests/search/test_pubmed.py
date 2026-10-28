from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kittum.core import sha256_hex
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.ledger.testing import InMemoryLedger
from kittum.search import (
    PubMedSource,
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
        "insert into project (name, policy_version) values ('search-test', 'v1') returning id"
    ).fetchone()
    assert row
    return row["id"]


def test_pubmed_recorded_search_end_to_end_into_postgres(pg: Tx) -> None:
    project_id = _seed_project(pg)
    strategy = "lumacaftor[ti] AND trial[tiab]"

    esearch_xml = (FIXTURES / "pubmed_esearch.xml").read_bytes()
    efetch_xml = (FIXTURES / "pubmed_efetch.xml").read_bytes()

    esearch_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        f"db=pubmed&term={strategy}&usehistory=y&retmode=xml&tool=kittum"
    )
    efetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
        "db=pubmed&query_key=1&WebEnv=MCID_6ae14800045d3e9e3004e3fd&retstart=0&retmax=24&retmode=xml&tool=kittum"
    )

    transport = FakeTransport(
        {
            esearch_url: esearch_xml,
            efetch_url: efetch_xml,
        }
    )
    gateway = PassThroughGateway(transport)
    artifacts = MemoryStore()
    source = PubMedSource(gateway, artifacts, tool="kittum")

    plan = SearchPlan(
        topic_id="cf-lumacaftor",
        source="pubmed",
        platform="PubMed",
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

    # 1. Correct counts and query translation
    assert execution.reported_count == 24
    assert execution.retrieved_count == 24
    assert execution.query_translation == '"lumacaftor"[Title] AND "trial"[Title/Abstract]'
    assert len(execution.records) == 24

    # 2. Structured abstracts with labels kept
    record_with_structured_abstract = next(
        r for r in execution.records if r.provider_record_id == "40551045"
    )
    assert record_with_structured_abstract.abstract is not None
    assert "BACKGROUND AND OBJECTIVE:" in record_with_structured_abstract.abstract
    assert "METHODS:" in record_with_structured_abstract.abstract
    assert "RESULTS:" in record_with_structured_abstract.abstract
    assert "CONCLUSIONS:" in record_with_structured_abstract.abstract

    # 3. DOIs and PMIDs
    assert record_with_structured_abstract.pmid == "40551045"
    assert record_with_structured_abstract.doi == "10.1007/s40268-025-00514-9"

    # 4. Registry IDs from DataBankList and abstract text
    assert "NCT05968612" in record_with_structured_abstract.registry_ids

    # Check another article with multiple authors and registry IDs
    art_33600745 = next(r for r in execution.records if r.provider_record_id == "33600745")
    assert len(art_33600745.authors) > 0
    assert "NCT02807415" in art_33600745.registry_ids
    assert art_33600745.year == 2021
    assert art_33600745.journal is not None

    # 5. Raw response artifacts stored
    esearch_hash = sha256_hex(esearch_xml)
    efetch_hash = sha256_hex(efetch_xml)
    assert esearch_hash in execution.raw_artifact_hashes
    assert efetch_hash in execution.raw_artifact_hashes
    assert artifacts.exists(esearch_hash)
    assert artifacts.exists(efetch_hash)

    # 6. Occurrence IDs assigned
    for r in execution.records:
        assert r.occurrence_id is not None

    # 7. Persistence into Postgres: load back and verify
    loaded = get_execution(pg, project_id, execution.id)
    assert loaded is not None
    assert loaded.id == execution.id
    assert loaded.reported_count == 24
    assert loaded.retrieved_count == 24
    assert loaded.query_translation == execution.query_translation
    assert len(loaded.records) == 24
    assert loaded.records[0].occurrence_id == execution.records[0].occurrence_id
    assert loaded.records[0].title == execution.records[0].title

    # 8. Ledger event recorded
    events = ledger.events(pg, project_id)
    assert len(events) == 1
    assert events[0].type == "search.executed"
    assert events[0].payload["reported_count"] == 24
    assert events[0].payload["strategy"] == strategy
