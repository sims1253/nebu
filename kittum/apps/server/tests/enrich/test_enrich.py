"""Enrichment against recorded real responses. No test here touches the network."""

from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest

from kittum.enrich import (
    Enrichment,
    EnrichmentConfig,
    RecordEnrichment,
    SourceFailure,
    enrich,
    full_text_candidates,
)
from tests.enrich.support import config as enrichment_config
from tests.enrich.support import doi_only_records, replay_gateway, source_records

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx
    from kittum.gateway import Gateway, HttpRequest, HttpResponse
    from kittum.search import SourceRecord

pytestmark = pytest.mark.postgres

_RETRACTED_PMID = "32450107"
_RETRACTION_NOTICE = "10.1016/s0140-6736(20)31324-6"


class _DownGateway:
    """A gateway whose named sources are out of budget."""

    def __init__(self, inner: Gateway, *sources: str) -> None:
        self._inner = inner
        self._sources = set(sources)

    def fetch(self, source: str, request: HttpRequest) -> HttpResponse:
        if source in self._sources:
            raise TimeoutError(f"{source} is out of budget")
        return self._inner.fetch(source, request)


def _project(tx: Tx) -> uuid.UUID:
    row = tx.execute(
        "insert into project (name, policy_version) values ('wp06', '1') returning id"
    ).fetchone()
    assert row
    return row["id"]


def _by_pmid(records: tuple[RecordEnrichment, ...]) -> dict[str, RecordEnrichment]:
    return {record.pmid: record for record in records if record.pmid}


def _enrich(
    tx: Tx,
    records: tuple[SourceRecord, ...],
    gateway: Gateway | None = None,
    config: EnrichmentConfig | None = None,
) -> Enrichment:
    if gateway is None:
        gateway, _ = replay_gateway()
    return enrich(tx, _project(tx), records, gateway, config=config or enrichment_config())


def test_enriches_trial_papers_and_stores_every_occurrence(pg: Tx) -> None:
    project = _project(pg)
    gateway, _ = replay_gateway()
    result = enrich(pg, project, source_records(), gateway, config=enrichment_config())

    assert result.failures == ()
    assert len(result.records) == 4
    by_pmid = _by_pmid(result.records)

    retracted = by_pmid[_RETRACTED_PMID]
    assert retracted.doi == "10.1016/s0140-6736(20)31180-6"
    assert retracted.pmcid == "PMC7255293"
    assert "hydroxychloroquine" in (retracted.abstract or "").lower()
    assert ("abstract", "europepmc") in {
        (source.field, source.source) for source in retracted.field_sources
    }

    # The BNT162b2 paper arrived with only a PMID; enrichment resolved the DOI and abstract.
    bnt = by_pmid["33301246"]
    assert bnt.doi == "10.1056/nejmoa2034577"
    assert "severe acute respiratory syndrome" in (bnt.abstract or "").lower()
    assert {("doi", "openalex"), ("abstract", "openalex")} <= {
        (source.field, source.source) for source in bnt.field_sources
    }

    # The CC BY protocol links the paper to its registry entry.
    bmj = by_pmid["42785935"]
    assert bmj.clinical_trial_numbers == ("NCT06804304",)
    assert ("clinical_trial_numbers", "crossref") in {
        (source.field, source.source) for source in bmj.field_sources
    }

    # Entity offsets point into the title plus abstract text and carry a normalised ID.
    source = next(record for record in source_records() if record.pmid == _RETRACTED_PMID)
    text = f"{source.title}\n{retracted.abstract}"
    drugs = [entity for entity in retracted.entities if entity.label == "drug"]
    assert any(text[entity.start : entity.end].lower() == "hydroxychloroquine" for entity in drugs)
    assert any(entity.normalized_id == "MESH:D006886" for entity in drugs)

    rows = pg.execute(
        "select * from record_enrichment where project_id = %s", (project,)
    ).fetchall()
    assert len(rows) == 4
    row = next(row for row in rows if row["pmid"] == _RETRACTED_PMID)
    assert row["doi"] == "10.1016/s0140-6736(20)31180-6"
    assert row["retracted"] is True
    assert row["retraction_notice_doi"] == _RETRACTION_NOTICE
    assert any(location["source"] == "europepmc" for location in row["oa_locations"])
    assert any(entity["label"] == "drug" for entity in row["entities"])
    assert {"field": "retraction", "source": "crossref"} in row["field_sources"]

    # Re-enrichment after an update search replaces the occurrence instead of duplicating it.
    gateway, _ = replay_gateway()
    enrich(pg, project, source_records(), gateway, config=enrichment_config())
    count = pg.execute(
        "select count(*) as n from record_enrichment where project_id = %s", (project,)
    ).fetchone()
    assert count == {"n": 4}


def test_retracted_paper_is_flagged_with_the_crossref_notice_doi(pg: Tx) -> None:
    result = _enrich(pg, source_records())
    by_pmid = _by_pmid(result.records)

    retracted = by_pmid[_RETRACTED_PMID]
    assert retracted.retracted is True
    assert retracted.retraction_notice_doi == _RETRACTION_NOTICE
    assert any(
        update.doi == _RETRACTION_NOTICE
        and update.kind == "retraction"
        and update.direction == "updated_by"
        for update in retracted.updates
    )
    # Corrections and expressions of concern arrive through the same Crossref links.
    assert any(
        update.kind in {"correction", "erratum", "expression_of_concern"}
        for update in retracted.updates
    )
    assert not any(record.retracted for record in result.records if record.pmid != _RETRACTED_PMID)


def test_batches_identifiers_per_source(pg: Tx) -> None:
    gateway, transport = replay_gateway()
    records = doi_only_records()
    result = _enrich(pg, records, gateway)

    urls = [request.url for request in transport.sent]
    openalex_urls = [url for url in urls if "api.openalex.org/works?filter=doi:" in url]
    assert len(openalex_urls) == 1
    filter_value = parse_qs(urlsplit(openalex_urls[0]).query)["filter"][0]
    assert set(filter_value.removeprefix("doi:").split("|")) == {
        record.doi.lower() for record in records if record.doi
    }

    europepmc_urls = [url for url in urls if "europepmc/webservices/rest/search" in url]
    assert len(europepmc_urls) == 1
    query = parse_qs(urlsplit(europepmc_urls[0]).query)["query"][0]
    assert query.count(" OR ") == len(records) * 2 - 1

    pubtator_urls = [url for url in urls if "pubtator3-api" in url]
    assert len(pubtator_urls) == 1
    pmids = parse_qs(urlsplit(pubtator_urls[0]).query)["pmids"][0].split(",")
    assert set(pmids) == {record.pmid for record in result.records if record.pmid}
    assert len(pmids) == len(records)
    assert {record.pmid for record in result.records} == {
        "32450107",
        "32678530",
        "42785935",
    }

    # Crossref has no OR filter, so retraction checks stay one request per DOI.
    crossref_urls = [url for url in urls if "api.crossref.org/works/" in url]
    assert len(crossref_urls) == len(records)


def test_openalex_outage_falls_back_to_europe_pmc(pg: Tx) -> None:
    gateway, _ = replay_gateway()
    result = _enrich(pg, source_records(), _DownGateway(gateway, "openalex"))

    assert result.failures == (
        SourceFailure(source="openalex", reason="TimeoutError: openalex is out of budget"),
    )
    by_pmid = _by_pmid(result.records)
    bnt = by_pmid["33301246"]
    assert bnt.doi == "10.1056/nejmoa2034577"
    pairs = {(source.field, source.source) for source in bnt.field_sources}
    assert ("doi", "europepmc") in pairs
    assert ("abstract", "europepmc") in pairs
    assert "openalex" not in bnt.sources_used
    assert by_pmid[_RETRACTED_PMID].retracted is True


def test_crossref_outage_falls_back_to_the_openalex_retraction_flag(pg: Tx) -> None:
    gateway, _ = replay_gateway()
    result = _enrich(pg, source_records(), _DownGateway(gateway, "crossref"))

    assert {failure.source for failure in result.failures} == {"crossref"}
    retracted = next(record for record in result.records if record.pmid == _RETRACTED_PMID)
    assert retracted.retracted is True
    assert retracted.retraction_notice_doi is None
    assert ("retraction", "openalex") in {
        (source.field, source.source) for source in retracted.field_sources
    }


def test_unpaywall_answers_when_metadata_sources_are_out_of_budget(pg: Tx) -> None:
    gateway, _ = replay_gateway()
    result = _enrich(pg, doi_only_records(), _DownGateway(gateway, "openalex", "europepmc"))

    assert {failure.source for failure in result.failures} == {"openalex", "europepmc"}
    bmj = next(record for record in result.records if record.doi == "10.1136/bmjopen-2026-124110")
    assert bmj.oa_locations
    assert all(location.source == "unpaywall" for location in bmj.oa_locations)
    candidates = full_text_candidates(bmj)
    assert candidates[0].source == "unpaywall"
    assert candidates[0].format == "pdf"
    assert candidates[0].licence == "cc-by"
    assert ("oa_locations", "unpaywall") in {
        (source.field, source.source) for source in bmj.field_sources
    }

    # Retraction checks still come from Crossref, which was healthy.
    retracted = next(
        record for record in result.records if record.doi == "10.1016/s0140-6736(20)31180-6"
    )
    assert retracted.retracted is True
    assert retracted.retraction_notice_doi == _RETRACTION_NOTICE


def test_full_text_candidates_follow_the_waterfall(pg: Tx) -> None:
    result = _enrich(pg, source_records())
    by_pmid = _by_pmid(result.records)

    bmj = by_pmid["42785935"]
    candidates = full_text_candidates(bmj)
    assert [candidate.source for candidate in candidates][:2] == ["europepmc", "europepmc"]
    assert [candidate.format for candidate in candidates][:2] == ["xml", "pdf"]
    assert candidates[0].licence == "cc by"
    ranks = [{"europepmc": 0, "unpaywall": 1, "openalex": 2}[c.source] for c in candidates]
    assert ranks == sorted(ranks)

    # The retracted paper has no Europe PMC PDF, so OpenAlex's publisher PDF follows the XML.
    retracted = by_pmid[_RETRACTED_PMID]
    retracted_candidates = full_text_candidates(retracted)
    assert retracted_candidates[0].source == "europepmc"
    assert retracted_candidates[0].format == "xml"
    assert retracted_candidates[-1].source == "openalex"
    assert retracted_candidates[-1].format == "pdf"
