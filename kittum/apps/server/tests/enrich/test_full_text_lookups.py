"""Europe PMC through its outage, and the full-text sources the waterfall asks one report at a time.

Recorded by `record_lookups.py`: Europe PMC's answers to the 24 PMIDs and 24 DOIs of the
28 October 2026 live run (the 503s it sent during its outage, then today's answer), medRxiv and
bioRxiv preprints, and an arXiv paper. Nothing touches the network.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest

from kittum.core import FixedClock
from kittum.enrich import (
    EnrichmentConfig,
    SourceUnavailable,
    WorkIds,
    europepmc,
    lookup_applies,
    lookup_full_text,
    skipped_source,
)
from kittum.gateway import RecordedTransport, SourceGateway
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.storage import MemoryStore
from tests.documents.conftest import savepoint_db
from tests.enrich.record_lookups import ARXIV, BIORXIV, LIVE_RUN_PMIDS, LOOKUPS, MEDRXIV
from tests.enrich.support import CONTACT_EMAIL

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.gateway import HttpRequest, HttpResponse


class _Seen:
    def __init__(self) -> None:
        self.inner = RecordedTransport(LOOKUPS, contact_email=CONTACT_EMAIL)
        self.urls: list[str] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.urls.append(request.url)
        return self.inner.send(request)


@pytest.mark.postgres
def test_europe_pmc_fails_in_its_own_words_then_answers_once_per_pmid(pg: Tx) -> None:
    transport = _Seen()
    clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    db = savepoint_db(pg)
    gateway = SourceGateway(
        database=db,
        transport=transport,
        artifacts=MemoryStore(),
        buckets="memory",
        cache="memory",
        clock=clock,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    manifest = json.loads((LOOKUPS / "manifest.json").read_text())
    (url,) = [u for u in manifest if "europepmc" in u]
    query = parse_qs(urlsplit(url).query)["query"][0]
    dois = [t.removeprefix('DOI:"').removesuffix('"') for t in query.split(" OR ") if "DOI" in t]

    # During the outage: five attempts, each a 503, and the failure says what Europe PMC said.
    with pytest.raises(SourceUnavailable) as failed:
        europepmc.works(gateway, pmids=LIVE_RUN_PMIDS, dois=dois, batch_size=100)
    assert (failed.value.status, failed.value.kind, failed.value.reason) == (
        503,
        "http_error",
        "HTTP 503 after 5 attempts",
    )
    assert failed.value.message == "Search service is temporarily unavailable. Please retry later."

    # Asked again once it is back. Each PMID is scoped to its MEDLINE record; a DOI is not, so a
    # preprint still matches. Europe PMC finds 25 records: one DOI also matches a copy of the same
    # article in PMC without a PMID or DOI, which names no work and is left out.
    works = europepmc.works(gateway, pmids=LIVE_RUN_PMIDS, dois=dois, batch_size=100)
    terms = parse_qs(urlsplit(transport.urls[-1]).query)["query"][0].split(" OR ")
    assert terms[: len(LIVE_RUN_PMIDS)] == [f"(EXT_ID:{p} AND SRC:MED)" for p in LIVE_RUN_PMIDS]
    assert terms[len(LIVE_RUN_PMIDS) :] == [f'DOI:"{d}"' for d in dois]
    body = json.loads((LOOKUPS / manifest[url][-1]["file"]).read_bytes())
    assert body["hitCount"] == 25
    assert sorted(w.pmid for w in works if w.pmid) == sorted(LIVE_RUN_PMIDS)
    assert len(works) == 24


def test_preprint_servers_and_arxiv_name_their_pdfs_with_licences_and_core_needs_a_key() -> None:
    gateway = PassThroughGateway(RecordedTransport(LOOKUPS, contact_email=CONTACT_EMAIL))
    config = EnrichmentConfig(contact_email=CONTACT_EMAIL)

    def named(source: str, doi: str) -> list[tuple[str, str | None, str | None]]:
        found = lookup_full_text(gateway, source, WorkIds(doi=doi), config=config)
        assert found is not None
        return [(loc.url, loc.licence, loc.version) for loc in found.locations]

    assert named("biorxiv", MEDRXIV) == [
        (f"https://www.medrxiv.org/content/{MEDRXIV}v2.full.pdf", "cc-by-nc-nd", "submitted")
    ]
    # medRxiv has no such preprint, so bioRxiv is asked, and its newest version answers.
    assert named("biorxiv", BIORXIV) == [
        (f"https://www.biorxiv.org/content/{BIORXIV}v2.full.pdf", "cc-by-nc-nd", "submitted")
    ]
    assert named("arxiv", ARXIV) == [
        (
            "https://export.arxiv.org/pdf/2101.00001",
            "http://creativecommons.org/licenses/by/4.0/",
            "submitted",
        )
    ]
    # A journal article is not asked of the preprint servers.
    journal = WorkIds(doi="10.1186/s12916-026-04943-4", pmid="42174613")
    assert [s for s in ("biorxiv", "arxiv") if lookup_applies(s, journal, config=config)] == []

    # CORE without a key is skipped, and says why; with one, the key goes in a header (never into
    # a URL or a recording) and a repository copy of the same DOI answers.
    assert skipped_source("core", config) == "no CORE key is set (KITTUM_CORE_API_KEY)"
    assert not lookup_applies("core", journal, config=config)
    assert lookup_full_text(gateway, "core", journal, config=config) is None
    keyed = config.model_copy(update={"core_api_key": "a-core-key-of-some-length"})
    # Shaped as CORE documents its search answer: no key was at hand to record a real one.
    answer = {
        "totalHits": 1,
        "results": [
            {
                "doi": "10.1186/S12916-026-04943-4",
                "downloadUrl": "https://core.ac.uk/download/1.pdf",
            }
        ],
    }
    core = FakeTransport(
        {
            "https://api.core.ac.uk/v3/search/works?q=doi%3A%2210.1186%2Fs12916-026-04943-4%22&limit=5": json.dumps(
                answer
            ).encode()
        }
    )
    found = lookup_full_text(PassThroughGateway(core), "core", journal, config=keyed)
    assert found is not None
    assert [(loc.url, loc.licence) for loc in found.locations] == [
        ("https://core.ac.uk/download/1.pdf", None)
    ]
    (sent,) = core.sent
    assert ("Authorization", "Bearer a-core-key-of-some-length") in sent.headers
    assert "a-core-key" not in sent.url
