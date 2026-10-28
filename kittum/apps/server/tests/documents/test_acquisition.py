"""Fetching full text through the gateway's waterfall and recording where it came from."""

from datetime import UTC, datetime

from kittum.core import FixedClock, new_id
from kittum.documents import (
    ACQUIRED,
    NOT_ACQUIRED,
    FullTextLocation,
    acquire,
    acquisitions,
)
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.ledger.testing import InMemoryLedger
from kittum.storage import MemoryStore
from tests.documents.conftest import LESOTHO, new_project

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
    assert pg.execute("select count(*) as n from document").fetchone() == {"n": 0}
