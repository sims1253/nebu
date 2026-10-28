"""Whose answer it is, and under which licence a publisher's PDF may be fetched.

On real responses recorded for the stack (`fixtures/stack`): a source that answers for another
work than the one asked about is a failure for that record, and nothing of its answer is used; a
Crossref PDF link counts only under a Creative Commons licence for exactly its content version,
from the day that licence starts. No test here touches the network.
"""

import json
import uuid
from datetime import date

import pytest

from kittum.core import sha256_hex
from kittum.enrich import (
    crossref,
    europepmc,
    full_text_candidates,
    lookup,
    openalex,
    unpaywall,
)
from kittum.gateway import HttpRequest, HttpResponse, RecordedTransport
from kittum.gateway.testing import PassThroughGateway, response
from kittum.search import SourceRecord
from tests.enrich.support import CONTACT_EMAIL, FIXTURES
from tests.enrich.support import config as enrichment_config

STACK = FIXTURES / "stack"
LESOTHO = "10.1186/s12916-026-04943-4"
VITAMIN_C = "10.1186/s12916-026-05037-x"
VITAMIN_C_PMID = "42399956"
VITAMIN_C_PMCID = "PMC13613656"


def _record(number: int, doi: str, title: str) -> SourceRecord:
    return SourceRecord(
        occurrence_id=uuid.UUID(int=number),
        source="citation_search",
        provider_record_id=f"CIT-{number}",
        ordinal=number,
        title=title,
        year=2026,
        journal="BMC Medicine",
        doi=doi,
        raw_hash=sha256_hex(doi.encode()),
    )


# The citation search's two trials, as `tests.app.record_stack` recorded them.
RECORDS = (
    _record(1, LESOTHO, "Community health worker-led versus facility-based type 2 diabetes care"),
    _record(2, VITAMIN_C, "Effect of Vitamin C on bleeding and pain after kidney biopsy"),
)


class Swapped:
    """The recorded sources, with some URLs answered by another URL's recorded body."""

    def __init__(self, answers: dict[str, bytes]) -> None:
        self.recorded = RecordedTransport(STACK)
        self.answers = answers

    def send(self, request: HttpRequest) -> HttpResponse:
        if request.url in self.answers:
            return response(200, self.answers[request.url], (("Content-Type", "application/json"),))
        return self.recorded.send(request)


def _body(url: str) -> bytes:
    return RecordedTransport(STACK).send(HttpRequest(url=url)).body


def _epmc_url() -> str:
    """The Europe PMC search for exactly the two trials (the demo's corpus has them too)."""
    manifest = json.loads((STACK / "manifest.json").read_text())
    return min(
        (u for u in manifest if u.startswith(europepmc.BASE) and "05037-x" in u),
        key=len,
    )


def test_an_answer_for_another_work_is_a_failure_and_none_of_it_is_used() -> None:
    # Crossref and Unpaywall answer the Lesotho trial's DOI with the Vitamin C trial's record, and
    # Europe PMC lists the Lesotho DOI with the other trial's PMID and PMCID.
    epmc = json.loads(_body(_epmc_url()))
    for entry in epmc["resultList"]["result"]:
        if entry.get("doi", "").lower() == LESOTHO:
            entry["pmid"], entry["pmcid"] = VITAMIN_C_PMID, VITAMIN_C_PMCID
    swapped = Swapped(
        {
            crossref._url(LESOTHO, CONTACT_EMAIL): _body(crossref._url(VITAMIN_C, CONTACT_EMAIL)),
            unpaywall._url(LESOTHO, CONTACT_EMAIL): _body(unpaywall._url(VITAMIN_C, CONTACT_EMAIL)),
            _epmc_url(): json.dumps(epmc).encode(),
        }
    )
    lesotho, vitamin_c = lookup(
        RECORDS, PassThroughGateway(swapped), config=enrichment_config()
    ).records

    reasons = {f.source: f.reason for f in lesotho.failures}
    assert reasons == {
        "crossref": f"answered for DOI {VITAMIN_C}, not {LESOTHO}",
        "unpaywall": f"answered for DOI {VITAMIN_C}, not {LESOTHO}",
        "europepmc": f"answered for PMID {VITAMIN_C_PMID}, not 42174613",
    }
    assert lesotho.pmcid is None  # the other trial's PMCID was not taken
    assert not any("05037" in o.url or VITAMIN_C_PMCID in o.url for o in lesotho.oa_locations)
    urls = [c.url for c in full_text_candidates(lesotho, on=date(2026, 10, 28))]
    assert urls == []  # OpenAlex's own locations for it are landing pages
    # The other trial, asked in the same batch, is untouched.
    assert vitamin_c.failures == ()
    assert vitamin_c.pmcid == VITAMIN_C_PMCID
    assert f"https://link.springer.com/content/pdf/{VITAMIN_C}.pdf" in [
        c.url for c in full_text_candidates(vitamin_c, on=date(2026, 10, 28))
    ]


def _crossref_links(message_edit) -> tuple:
    body = json.loads(_body(crossref._url(LESOTHO, CONTACT_EMAIL)))
    message_edit(body["message"])
    gateway = PassThroughGateway(
        Swapped({crossref._url(LESOTHO, CONTACT_EMAIL): json.dumps(body).encode()})
    )
    work = crossref.work(gateway, LESOTHO, email=CONTACT_EMAIL)
    assert work is not None
    return work.pdf_links


def _candidates(links: tuple, on: date) -> list[tuple[str, str | None]]:
    from kittum.enrich import RecordEnrichment

    record = RecordEnrichment(
        occurrence_id=uuid.UUID(int=1), provider_record_id="x", oa_locations=links
    )
    return [(c.url, c.licence) for c in full_text_candidates(record, on=on)]


PDF = f"https://link.springer.com/content/pdf/{LESOTHO}.pdf"


def test_a_publisher_pdf_needs_a_creative_commons_licence_for_its_own_version_in_effect() -> None:
    # As recorded: CC BY for text and data mining from 22 May 2026, and for the published version
    # (`vor`, the version of the PDF link) from 16 July 2026.
    links = _crossref_links(lambda _: None)
    (link,) = links
    assert (link.licence, link.licence_start, link.version) == (
        "cc-by-4.0",
        date(2026, 7, 16),
        "published",
    )
    assert _candidates(links, date(2026, 10, 28)) == [(PDF, "cc-by-4.0")]
    # Before the published version's licence starts, its PDF is no candidate, although the text
    # and data mining licence is in effect.
    assert _candidates(links, date(2026, 7, 15)) == []

    # A licence for text and data mining alone does not open the published version.
    def tdm_only(message: dict) -> None:
        message["license"] = [
            entry for entry in message["license"] if entry["content-version"] == "tdm"
        ]

    assert _crossref_links(tdm_only) == ()

    # The licence for the link's own version is the one carried, not another version's.
    def nc_for_tdm(message: dict) -> None:
        for entry in message["license"]:
            if entry["content-version"] == "tdm":
                entry["URL"] = "https://creativecommons.org/licenses/by-nc/4.0"

    assert [link.licence for link in _crossref_links(nc_for_tdm)] == ["cc-by-4.0"]


DRESSES = {
    "https": lambda doi: f"https://doi.org/{doi}",
    "dx": lambda doi: f"http://dx.doi.org/{doi}",
    "bare host": lambda doi: f"doi.org/{doi}",
    "doi scheme, upper case, spaces": lambda doi: f"  doi:{doi.upper()} ",
}


@pytest.mark.parametrize("dress", DRESSES.values(), ids=DRESSES.keys())
def test_a_doi_in_any_dress_is_the_same_work_to_every_source(dress) -> None:
    gateway = PassThroughGateway(RecordedTransport(STACK))
    bare = lookup(RECORDS, gateway, config=enrichment_config()).records
    dressed = tuple(r.model_copy(update={"doi": dress(r.doi)}) for r in RECORDS)
    found = lookup(dressed, gateway, config=enrichment_config()).records
    for plain, record in zip(bare, found, strict=True):
        # The same requests (the recordings answer them), the same answers, no failure.
        assert record.failures == ()
        assert record.sources_asked == (
            "openalex",
            "europepmc",
            "crossref",
            "unpaywall",
            "pubtator",
        )
        assert record.doi == plain.doi == plain.doi.casefold()
        assert (record.pmid, record.pmcid, record.abstract) == (
            plain.pmid,
            plain.pmcid,
            plain.abstract,
        )
        assert record.oa_locations == plain.oa_locations
        assert record.field_sources == plain.field_sources
    # The clients that compare an answer with the DOI asked about take any dress too.
    for record in dressed:
        assert record.doi is not None
        assert crossref.work(gateway, record.doi, email=CONTACT_EMAIL) is not None
        assert unpaywall.best_oa_location(gateway, record.doi, email=CONTACT_EMAIL) is not None


def test_a_prefixed_doi_with_its_pmid_keeps_its_abstract_and_fails_no_source() -> None:
    """The reported case: a DOI with its resolver prefix, the PMID, and the search's abstract.
    OpenAlex answers with the bare DOI and the same PMID: the same work."""
    abstract = "BACKGROUND: Type 2 diabetes poses a growing public health burden."
    records = (
        RECORDS[0].model_copy(
            update={"doi": f"https://doi.org/{LESOTHO}", "pmid": "42174613", "abstract": abstract}
        ),
        RECORDS[1].model_copy(update={"pmid": VITAMIN_C_PMID}),
    )
    # OpenAlex is asked by DOI and, now that the PMIDs are known, by PMID; it answers both with
    # the same two works (the PMID request was not recorded, the DOI request was).
    by_doi = _body(openalex._url(f"doi:{LESOTHO}|{VITAMIN_C}"))
    swapped = Swapped({openalex._url(f"pmid:42174613|{VITAMIN_C_PMID}"): by_doi})
    lesotho, _ = lookup(records, PassThroughGateway(swapped), config=enrichment_config()).records
    assert lesotho.failures == ()
    assert lesotho.doi == LESOTHO
    assert lesotho.abstract == abstract
    assert ("abstract", "citation_search") in {(f.field, f.source) for f in lesotho.field_sources}
    assert "openalex" in lesotho.sources_used and "europepmc" in lesotho.sources_used
