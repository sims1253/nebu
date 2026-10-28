"""Fetching full text through the gateway's waterfall and recording where it came from."""

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pymupdf

from kittum.core import FixedClock, new_id, sha256_hex
from kittum.documents import (
    ACQUIRED,
    NOT_ACQUIRED,
    ExpectedReport,
    FullTextLocation,
    acquire,
    acquisitions,
    check_report,
)
from kittum.enrich import EnrichmentConfig, WorkIds, lookup_full_text
from kittum.gateway import RecordedTransport, SourceGateway
from kittum.gateway.testing import FakeTransport, PassThroughGateway, response
from kittum.ledger.testing import InMemoryLedger
from kittum.storage import MemoryStore
from tests.documents.conftest import FIXTURES, LESOTHO, new_project, savepoint_db
from tests.enrich.support import CONTACT_EMAIL

if TYPE_CHECKING:
    from kittum.gateway import HttpRequest, HttpResponse

EUROPE_PMC = "https://europepmc.org/articles/PMC13374172?pdf=render"
UNPAYWALL = "https://bmcmedicine.biomedcentral.com/counter/pdf/10.1186/s12916-026-04943-4"
OPENALEX = "https://doi.org/10.1186/s12916-026-04943-4"
WHEN = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)


def test_waterfall_keeps_the_first_pdf_and_records_source_licence_and_attempts(pg):
    project = new_project(pg)
    actor = new_id()
    pdf = LESOTHO.read_bytes()
    transport = FakeTransport(
        {
            # A landing page where a PDF was promised, then the PDF.
            UNPAYWALL: b"<!DOCTYPE html><html><body>Download PDF</body></html>",
            OPENALEX: pdf,
        }
    )
    artifacts, ledger = MemoryStore(), InMemoryLedger(FixedClock(WHEN))
    waterfall = [
        FullTextLocation(source="europepmc", url=EUROPE_PMC, licence="cc by"),
        FullTextLocation(source="unpaywall", url=UNPAYWALL, licence="cc-by"),
        FullTextLocation(
            source="openalex",
            url=OPENALEX,
            licence="cc-by",
            terms_url="https://creativecommons.org/licenses/by/4.0/",
        ),
    ]

    outcome = acquire(
        pg,
        gateway=PassThroughGateway(transport),
        artifacts=artifacts,
        ledger=ledger,
        clock=FixedClock(WHEN),
        project_id=project,
        actor_id=actor,
        locations=waterfall,
    )

    assert [r.url for r in transport.sent] == [EUROPE_PMC, UNPAYWALL, OPENALEX]
    assert [(a.source, a.status, a.outcome) for a in outcome.attempts] == [
        ("europepmc", 404, "http_error"),
        ("unpaywall", 200, "not_pdf"),
        ("openalex", 200, "acquired"),
    ]
    acquisition = outcome.acquisition
    assert acquisition is not None
    assert (acquisition.source, acquisition.licence) == ("openalex", "cc-by")
    assert acquisition.document.page_count == 13
    assert artifacts.get(acquisition.document.artifact_hash) == pdf

    (event,) = ledger.events(pg, project)
    assert event.type == ACQUIRED
    assert event.payload["document_id"] == str(acquisition.document.id)
    assert event.payload["terms_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert [a["outcome"] for a in event.payload["attempts"]] == [
        "http_error",
        "not_pdf",
        "acquired",
    ]
    (row,) = acquisitions(pg, acquisition.document.id)
    assert (row["source"], row["licence"], row["event_id"]) == ("openalex", "cc-by", event.event_id)
    assert row["acquired_at"] == WHEN

    # The same bytes from another source are the same document with a second acquisition.
    again = acquire(
        pg,
        gateway=PassThroughGateway(FakeTransport({EUROPE_PMC: pdf})),
        artifacts=artifacts,
        ledger=ledger,
        clock=FixedClock(WHEN),
        project_id=project,
        actor_id=actor,
        locations=waterfall[:1],
    )
    assert again.acquisition is not None
    assert again.acquisition.document.id == acquisition.document.id
    assert [r["source"] for r in acquisitions(pg, acquisition.document.id)] == [
        "openalex",
        "europepmc",
    ]


def test_a_waterfall_without_a_pdf_is_recorded_as_failed(pg):
    project = new_project(pg)
    ledger = InMemoryLedger(FixedClock(WHEN))
    truncated = LESOTHO.read_bytes()[:2000]  # starts like a PDF, does not open as one
    # Other tests commit documents of their own, so count what this waterfall adds.
    documents = pg.execute("select count(*) as n from document").fetchone()
    outcome = acquire(
        pg,
        gateway=PassThroughGateway(FakeTransport({UNPAYWALL: truncated})),
        artifacts=MemoryStore(),
        ledger=ledger,
        clock=FixedClock(WHEN),
        project_id=project,
        actor_id=new_id(),
        locations=[
            FullTextLocation(source="europepmc", url=EUROPE_PMC),
            FullTextLocation(source="unpaywall", url=UNPAYWALL),
        ],
    )
    assert outcome.acquisition is None
    assert [a.outcome for a in outcome.attempts] == ["http_error", "unreadable_pdf"]
    (event,) = ledger.events(pg, project)
    assert event.type == NOT_ACQUIRED
    assert len(event.payload["attempts"]) == 2
    assert pg.execute("select count(*) as n from document").fetchone() == documents


JMIR = FIXTURES / "jmir"
JMIR_DOI = "10.2196/66709"
JMIR_TITLE = (
    "Using Text Messaging Ecological Momentary Assessment to Record Changes in e-Cigarette and"
    " Combustible Cigarette Use: Pilot Randomized Clinical Trial"
)


def test_a_supplement_named_as_the_article_is_a_wrong_document_and_the_waterfall_moves_on(pg):
    """Issue #60, on the real case. Unpaywall names a JMIR trial's CONSORT checklist, a supplement,
    as the trial's PDF, and Semantic Scholar names the same file. The checklist shows neither the
    trial's DOI nor its title (only the trial's draft title, on its second page), so it is a wrong
    document and is not stored; PubMed Central's copy of the article is acquired instead."""
    project = new_project(pg)
    actor = new_id()
    gateway = PassThroughGateway(RecordedTransport(JMIR, contact_email=CONTACT_EMAIL))
    # A key opts in to Semantic Scholar; the recording answers whatever key is sent.
    config = EnrichmentConfig(
        contact_email=CONTACT_EMAIL, semantic_scholar_api_key="a-semantic-scholar-key"
    )
    ids = WorkIds(doi=JMIR_DOI, pmid="40116747")
    best = json.loads((JMIR / "unpaywall-10.2196-66709.json").read_bytes())["best_oa_location"]
    named = best["url_for_pdf"]
    assert "formative_v9i1e66709_app2.pdf" in named and best["license"] == "cc-by"
    scholar = lookup_full_text(gateway, "semanticscholar", ids, config=config)
    assert scholar is not None and [loc.url for loc in scholar.locations] == [named]
    pmc = lookup_full_text(gateway, "pmc", ids, config=config)
    assert pmc is not None
    (article,) = pmc.locations
    assert (article.licence, article.version) == ("CC BY", "published")

    artifacts, ledger = MemoryStore(), InMemoryLedger(FixedClock(WHEN))
    outcome = acquire(
        pg,
        gateway=gateway,
        artifacts=artifacts,
        ledger=ledger,
        clock=FixedClock(WHEN),
        project_id=project,
        actor_id=actor,
        locations=[
            FullTextLocation(source="unpaywall", url=named, licence=best["license"]),
            FullTextLocation(source="pmc", url=article.url, licence=article.licence),
        ],
        expected=ExpectedReport(doi=JMIR_DOI, title=JMIR_TITLE),
    )

    assert [(a.source, a.status, a.outcome, a.check) for a in outcome.attempts] == [
        (
            "unpaywall",
            200,
            "wrong_document",
            "The first pages show neither the record's DOI nor its title.",
        ),
        ("pmc", 200, "acquired", f"The PDF shows DOI {JMIR_DOI}."),
    ]
    assert outcome.acquisition is not None
    assert (outcome.acquisition.source, outcome.acquisition.document.page_count) == ("pmc", 13)
    checklist = sha256_hex((JMIR / "app2.pdf").read_bytes())
    stored = pg.execute("select 1 from document where artifact_hash = %s", (checklist,)).fetchone()
    assert stored is None
    (event,) = ledger.events(pg, project)
    assert event.type == ACQUIRED
    assert [a["outcome"] for a in event.payload["attempts"]] == ["wrong_document", "acquired"]


def _pdf(*lines: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "\n".join(lines), fontsize=11)
        return document.tobytes()


def test_a_pdf_is_the_records_report_only_by_its_whole_doi_or_its_whole_title():
    # A page that holds a fragment of the record's title does not show the title.
    fragment = _pdf("Clinical trial")
    title = "A clinical trial of a completely different medicine"
    assert check_report(fragment, doi=None, title=title).verdict == "mismatch"
    assert (
        check_report(
            _pdf("A clinical trial of a completely different", "medicine"), doi=None, title=title
        ).verdict
        == "match"
    )
    # A page that ends inside the title does not show it either.
    cut = _pdf("A clinical trial of a completely different medic")
    assert check_report(cut, doi=None, title=title).verdict == "mismatch"
    # Another DOI that starts with the record's is another work, on its line or the next.
    for other in (("10.1234/abc(20)12345",), ("10.1234/abc", "-def")):
        assert check_report(_pdf(*other), doi="10.1234/abc", title=None).verdict == "mismatch"
    # A DOI's own parentheses match, and a bracket around it closes outside it.
    lancet = "10.1016/S0140-6736(20)30183-5"
    for printed in (f"doi:{lancet}", f"(https://doi.org/{lancet})"):
        assert check_report(_pdf(printed), doi=lancet, title=None).by == "doi"
    longer = _pdf("Journal of Tests 2026", "doi: 10.1234/wrong-paper")
    assert check_report(longer, doi="10.1234/wrong", title=None).verdict == "mismatch"
    assert (
        check_report(_pdf("doi: 10.1234/wrong."), doi="10.1234/wrong", title=None).verdict
        == "match"
    )
    # A DOI the line break hyphenated is still the record's, and a DOI's own hyphen still matches.
    wrapped = _pdf("https://doi.org/10.1234/long-", "identifier")
    assert check_report(wrapped, doi="10.1234/longidentifier", title=None).by == "doi"
    assert check_report(wrapped, doi="10.1234/long-identifier", title=None).by == "doi"


class _Failing:
    """A source host that keeps answering 503, one that refuses with a reason, then the PDF."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request.url)
        if "down" in request.url:
            return response(
                503, b'{"errCode":503,"errMsg":"Search service is down.\\nRetry later."}'
            )
        if "refused" in request.url:
            return response(403, b"Automated downloads are not allowed from this address.")
        return response(200, LESOTHO.read_bytes())


def test_a_failed_download_keeps_what_the_source_said(pg):
    project = new_project(pg)
    clock = FixedClock(WHEN)
    transport = _Failing()
    gateway = SourceGateway(
        database=savepoint_db(pg),
        transport=transport,
        artifacts=MemoryStore(),
        buckets="memory",
        cache="memory",
        max_attempts=2,
        clock=clock,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    ledger = InMemoryLedger(clock)
    outcome = acquire(
        pg,
        gateway=gateway,
        artifacts=MemoryStore(),
        ledger=ledger,
        clock=clock,
        project_id=project,
        actor_id=new_id(),
        locations=[
            FullTextLocation(source="europepmc", url="https://down.example/a.pdf"),
            FullTextLocation(source="unpaywall", url="https://refused.example/b.pdf"),
            FullTextLocation(source="openalex", url="https://ok.example/c.pdf"),
        ],
    )
    assert [(a.outcome, a.status, a.message) for a in outcome.attempts] == [
        ("unavailable", 503, "Search service is down. Retry later."),
        ("http_error", 403, "Automated downloads are not allowed from this address."),
        ("acquired", 200, None),
    ]
    (event,) = ledger.events(pg, project)
    assert [a["message"] for a in event.payload["attempts"]][:2] == [
        "Search service is down. Retry later.",
        "Automated downloads are not allowed from this address.",
    ]
