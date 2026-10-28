"""Enrichment against recorded real responses. No test here touches the network."""

import uuid
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from kittum.core import sha256_hex
from kittum.enrich import (
    Enrichment,
    EnrichmentConfig,
    RecordEnrichment,
    SourceFailure,
    crossref,
    enrich,
    full_text_candidates,
    pubtator,
)
from kittum.gateway import HttpResponse
from kittum.search import SourceRecord
from tests.enrich.support import (
    CONTACT_EMAIL,
    DERIVED_RELATION_FIXTURE,
    FIXTURES,
    RETRACTION_NOTICE,
    RETRACTION_ORIGINAL,
    doi_only_records,
    non_oa_records,
    replay_gateway,
    source_records,
)
from tests.enrich.support import config as enrichment_config

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.gateway import Gateway, HttpRequest

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


class _OnlyCrossref:
    """A gateway that answers Crossref with one recorded body and nothing else."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def fetch(self, source: str, _request: HttpRequest) -> HttpResponse:
        if source != "crossref":
            raise TimeoutError(f"{source} is out of budget")
        return HttpResponse(
            status=200,
            headers=(),
            body=self._body,
            body_hash=sha256_hex(self._body),
        )


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


def test_entity_offsets_keep_repeated_mentions_distinct(pg: Tx) -> None:
    result = _enrich(pg, source_records())
    retracted = _by_pmid(result.records)[_RETRACTED_PMID]
    source = next(record for record in source_records() if record.pmid == _RETRACTED_PMID)
    text = f"{source.title}\n{retracted.abstract}"
    title_end = len(source.title)

    gateway, _ = replay_gateway()
    annotations = pubtator.annotations(gateway, ["32450107", "33301246", "32678530", "42785935"])[
        _RETRACTED_PMID
    ]
    expected_texts = {annotation.text for annotation in annotations}

    mentions = [
        entity
        for entity in retracted.entities
        if text[entity.start : entity.end].lower() == "hydroxychloroquine"
    ]
    assert len(mentions) > 1
    assert len({(entity.start, entity.end) for entity in mentions}) == len(mentions)

    in_title = [entity for entity in mentions if entity.start < title_end]
    assert [entity.start for entity in in_title] == [11]
    assert all(entity.start > title_end for entity in mentions if entity not in in_title)

    # Every span is exactly the annotation text, and none crosses the title/abstract seam.
    for entity in retracted.entities:
        assert text[entity.start : entity.end] in expected_texts
        assert entity.end <= title_end or entity.start >= title_end + 1


def test_bioc_offsets_map_chloroquine_mentions_to_whole_words(pg: Tx) -> None:
    result = _enrich(pg, source_records())
    record = _by_pmid(result.records)[_RETRACTED_PMID]
    source = next(item for item in source_records() if item.pmid == _RETRACTED_PMID)
    text = f"{source.title}\n{record.abstract}"

    spans = sorted(
        (entity.start, entity.end, text[entity.start : entity.end])
        for entity in record.entities
        if text[entity.start : entity.end].lower() in {"chloroquine", "hydroxychloroquine"}
    )
    assert spans == [
        (11, 29, "Hydroxychloroquine"),
        (33, 44, "chloroquine"),
        (146, 164, "Hydroxychloroquine"),
        (168, 179, "chloroquine"),
        (582, 600, "hydroxychloroquine"),
        (604, 615, "chloroquine"),
        (992, 1003, "chloroquine"),
        (1011, 1022, "chloroquine"),
        (1041, 1059, "hydroxychloroquine"),
        (1070, 1088, "hydroxychloroquine"),
        (1799, 1810, "chloroquine"),
        (1826, 1837, "chloroquine"),
        (1870, 1888, "hydroxychloroquine"),
        (1908, 1926, "hydroxychloroquine"),
        (2350, 2368, "hydroxychloroquine"),
        (2418, 2436, "hydroxychloroquine"),
        (2483, 2494, "chloroquine"),
        (2528, 2539, "chloroquine"),
        (2709, 2727, "hydroxychloroquine"),
        (2756, 2774, "hydroxychloroquine"),
        (2820, 2831, "chloroquine"),
        (2864, 2875, "chloroquine"),
        (3085, 3103, "hydroxychloroquine"),
        (3107, 3118, "chloroquine"),
    ]

    chloroquine = [span for span in spans if span[2].lower() == "chloroquine"]
    hydroxychloroquine = [span for span in spans if span[2].lower() == "hydroxychloroquine"]
    assert not any(
        outer[0] <= inner[0] and inner[1] <= outer[1]
        for outer in hydroxychloroquine
        for inner in chloroquine
    )


def test_retraction_notice_is_not_flagged_but_the_original_is() -> None:
    gateway, _ = replay_gateway()
    notice = crossref.work(gateway, RETRACTION_NOTICE, email=CONTACT_EMAIL)
    original = crossref.work(gateway, RETRACTION_ORIGINAL, email=CONTACT_EMAIL)
    assert notice is not None
    assert original is not None

    assert any(
        update.kind == "retraction" and update.direction == "updates" for update in notice.updates
    )
    assert crossref.retraction_notice(notice) is None

    assert crossref.retraction_notice(original) == RETRACTION_NOTICE.lower()


def test_incoming_is_retracted_by_relation_flags_the_work(pg: Tx) -> None:
    body = (FIXTURES / DERIVED_RELATION_FIXTURE).read_bytes()
    record = SourceRecord(
        occurrence_id=uuid.UUID(int=4001),
        source="crossref",
        provider_record_id=RETRACTION_ORIGINAL,
        ordinal=1,
        title="RETRACTED: Ileal-lymphoid-nodular hyperplasia, non-specific colitis.",
        doi=RETRACTION_ORIGINAL,
        raw_hash=sha256_hex(b"wp06-relation"),
    )
    result = _enrich(pg, (record,), _OnlyCrossref(body))

    assert any(failure.source == "openalex" for failure in result.failures)
    enriched = result.records[0]
    assert enriched.retracted is True
    assert enriched.retraction_notice_doi == RETRACTION_NOTICE.lower()
    assert ("retraction", "crossref") in {
        (source.field, source.source) for source in enriched.field_sources
    }


def test_unpaywall_is_asked_for_every_record_with_a_doi(pg: Tx) -> None:
    gateway, transport = replay_gateway()
    result = _enrich(pg, source_records(), gateway)

    # The waterfall asks Unpaywall even when Europe PMC or OpenAlex already offered a file.
    assert all(
        any(location.format in {"pdf", "xml"} for location in record.oa_locations)
        for record in result.records
    )
    requested = [
        request.url for request in transport.sent if "api.unpaywall.org/v2/" in request.url
    ]
    dois = {record.doi for record in result.records if record.doi}
    assert len(requested) == len(dois) == 4
    for doi in dois:
        assert any(unquote(urlsplit(url).path).lower().endswith(doi) for url in requested)
    for record in result.records:
        assert ("oa_locations", "unpaywall") in {
            (source.field, source.source) for source in record.field_sources
        }


def test_non_open_access_europepmc_record_gets_no_full_text_candidate(pg: Tx) -> None:
    result = _enrich(pg, non_oa_records())
    record = result.records[0]

    assert record.pmcid == "PMC13544592"  # Europe PMC indexes it, but not as open access
    assert not any(location.source == "europepmc" for location in record.oa_locations)
    assert all(candidate.source != "europepmc" for candidate in full_text_candidates(record))
