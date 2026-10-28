"""Recorded real-world inputs for the enrichment tests.

`record_fixtures.py` fills `fixtures/` by calling the live APIs once. Tests replay the same bytes
through `FakeTransport` and never touch the network.
"""

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from kittum.core import sha256_hex
from kittum.enrich import EnrichmentConfig
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.search import SourceRecord

if TYPE_CHECKING:
    from kittum.gateway import HttpRequest, HttpResponse

FIXTURES = Path(__file__).parent / "fixtures"
CONTACT_EMAIL = "test@kittum.dev"

_HCQ = {
    "provider_record_id": "32450107",
    "title": (
        "RETRACTED: Hydroxychloroquine or chloroquine with or without a macrolide for treatment "
        "of COVID-19: a multinational registry analysis."
    ),
    "journal": "Lancet",
    "doi": "10.1016/S0140-6736(20)31180-6",
    "pmid": "32450107",
}
_BNT = {
    "provider_record_id": "33301246",
    "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine.",
    "journal": "New England Journal of Medicine",
    "doi": None,
    "pmid": "33301246",
}
_DEX = {
    "provider_record_id": "32678530",
    "title": "Dexamethasone in Hospitalized Patients with Covid-19.",
    "journal": "New England Journal of Medicine",
    "doi": "10.1056/NEJMoa2021436",
    "pmid": "32678530",
}
_BMJ = {
    "provider_record_id": "42785935",
    "title": (
        "Efficacy of music-based telerehabilitation after stroke: protocol description of a "
        "randomised controlled trial."
    ),
    "journal": "BMJ Open",
    "doi": "10.1136/bmjopen-2026-124110",
    "pmid": "42785935",
}


def _record(number: int, raw: dict[str, object]) -> SourceRecord:
    return SourceRecord(
        occurrence_id=uuid.UUID(int=number),
        source="pubmed",
        provider_record_id=str(raw["provider_record_id"]),
        ordinal=number - 1000,
        title=str(raw["title"]),
        abstract=None,
        year=2020 if number != 1004 else 2026,
        journal=str(raw["journal"]),
        doi=raw["doi"] if isinstance(raw["doi"], str) else None,
        pmid=raw["pmid"] if isinstance(raw["pmid"], str) else None,
        raw_hash=sha256_hex(str(raw["provider_record_id"]).encode()),
    )


def source_records() -> tuple[SourceRecord, ...]:
    """Four real trial publications: one retracted, one with only a PMID, one CC BY protocol."""
    return tuple(
        _record(1000 + index, raw) for index, raw in enumerate((_HCQ, _BNT, _DEX, _BMJ), 1)
    )


def doi_only_records() -> tuple[SourceRecord, ...]:
    """The same trials with only a DOI, so identifier batching has to resolve the PMIDs."""
    return tuple(
        _record(2000 + index, {**raw, "pmid": None})
        for index, raw in enumerate((_HCQ, _DEX, _BMJ), 1)
    )


def config() -> EnrichmentConfig:
    return EnrichmentConfig(contact_email=CONTACT_EMAIL)


class _StrictFakeTransport(FakeTransport):
    """A replay transport that fails loudly when a test asks for an unrecorded URL."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        super().__init__(responses)
        self._urls = set(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.url not in self._urls:
            raise AssertionError(f"no recorded response for {request.url}")
        return super().send(request)


def replay_gateway() -> tuple[PassThroughGateway, FakeTransport]:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    responses = {url: (FIXTURES / filename).read_bytes() for url, filename in manifest.items()}
    transport = _StrictFakeTransport(responses)
    return PassThroughGateway(transport), transport
