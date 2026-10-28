from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kittum.core import sha256_hex
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.ledger.testing import InMemoryLedger
from kittum.search import (
    PubMedSource,
    SearchPlan,
    SourceError,
    get_execution,
    list_executions,
    run,
)
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).parent / "fixtures"


def test_pubmed_recorded_search_end_to_end_into_postgres(
    pg: Tx, project_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    strategy = "lumacaftor[ti] AND trial[tiab]"

    esearch_xml = (FIXTURES / "pubmed_esearch.xml").read_bytes()
    efetch_xml = (FIXTURES / "pubmed_efetch.xml").read_bytes()

    # The strategy is percent-encoded, so `#`, `&`, and spaces reach PubMed intact.
    esearch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        "db=pubmed&term=lumacaftor%5Bti%5D%20AND%20trial%5Btiab%5D&usehistory=y&retmode=xml&tool=kittum"
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
        actor_id=actor_id,
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

    # 5. Raw responses stored byte for byte, and every record points at its EFetch body
    esearch_hash = sha256_hex(esearch_xml)
    efetch_hash = sha256_hex(efetch_xml)
    assert execution.raw_artifact_hashes == (esearch_hash, efetch_hash)
    assert artifacts.get(esearch_hash) == esearch_xml
    assert artifacts.get(efetch_hash) == efetch_xml
    assert {r.raw_hash for r in execution.records} == {efetch_hash}

    # 6. Occurrence IDs are unique and survive the round trip
    assert len({r.occurrence_id for r in execution.records}) == 24

    # 7. Persistence into Postgres: load back and verify
    loaded = get_execution(pg, project_id, execution.id)
    assert loaded is not None
    assert loaded.id == execution.id
    assert loaded.reported_count == 24
    assert loaded.retrieved_count == 24
    assert loaded.query_translation == execution.query_translation
    assert len(loaded.records) == 24
    assert loaded.raw_artifact_hashes == execution.raw_artifact_hashes
    assert [r.occurrence_id for r in loaded.records] == [r.occurrence_id for r in execution.records]
    assert loaded.records[0].title == execution.records[0].title
    stored = pg.execute(
        "select count(*) as n from source_occurrence where execution_id = %s", (execution.id,)
    ).fetchone()
    assert stored and stored["n"] == execution.retrieved_count

    # 8. Ledger event recorded
    events = ledger.events(pg, project_id)
    assert len(events) == 1
    assert events[0].type == "search.executed"
    assert events[0].actor_id == actor_id
    assert events[0].payload["execution_id"] == str(execution.id)
    assert events[0].payload["reported_count"] == 24
    assert events[0].payload["retrieved_count"] == 24
    assert events[0].payload["strategy"] == strategy
    assert events[0].payload["query_translation"] == execution.query_translation
    assert events[0].payload["raw_artifact_hashes"] == [esearch_hash, efetch_hash]
    assert ledger.verify(pg, project_id).ok


def test_pubmed_error_reply_writes_nothing(
    pg: Tx, project_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    # The fake transport answers 404 for every URL, as PubMed would for a broken request.
    artifacts = MemoryStore()
    source = PubMedSource(PassThroughGateway(FakeTransport({})), artifacts)
    plan = SearchPlan(topic_id="t", source="pubmed", platform="PubMed", strategy="x")
    ledger = InMemoryLedger()

    with pytest.raises(SourceError, match="HTTP 404"):
        run(pg, project_id, plan, source, ledger=ledger, actor_id=actor_id, artifacts=artifacts)

    assert list_executions(pg, project_id) == []
    assert ledger.events(pg, project_id) == []


def test_pubmed_reads_registry_ids_from_databanklist(
    pg: Tx, project_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    # Recorded: PMID 40701924 names NCT03052283 in Article/DataBankList, nowhere in its text.
    efetch_xml = (FIXTURES / "pubmed_efetch_databank_only.xml").read_bytes()
    transport = FakeTransport(
        {
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
            "db=pubmed&term=40701924%5Bpmid%5D&usehistory=y&retmode=xml&tool=kittum": (
                FIXTURES / "pubmed_esearch_databank_only.xml"
            ).read_bytes(),
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
            "db=pubmed&query_key=1&WebEnv=MCID_6ae14f1a4cb2c746e30b1a31"
            "&retstart=0&retmax=1&retmode=xml&tool=kittum": efetch_xml,
        }
    )
    artifacts = MemoryStore()
    source = PubMedSource(PassThroughGateway(transport), artifacts)
    plan = SearchPlan(topic_id="t", source="pubmed", platform="PubMed", strategy="40701924[pmid]")

    execution = run(
        pg,
        project_id,
        plan,
        source,
        ledger=InMemoryLedger(),
        actor_id=actor_id,
        artifacts=artifacts,
    )

    (record,) = execution.records
    assert record.pmid == "40701924"
    assert "NCT03052283" not in f"{record.title} {record.abstract}"
    assert "NCT03052283" in record.registry_ids
    loaded = get_execution(pg, project_id, execution.id)
    assert loaded is not None
    assert "NCT03052283" in loaded.records[0].registry_ids
