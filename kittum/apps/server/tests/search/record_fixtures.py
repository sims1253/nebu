"""Script to record real API fixtures from PubMed, Europe PMC, and ClinicalTrials.gov."""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx2

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def record_pubmed() -> None:
    term = "lumacaftor[ti] AND trial[tiab]"
    esearch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        f"db=pubmed&term={httpx2.URL('http://x', params={'q': term}).params['q']}&usehistory=y&retmode=xml&tool=kittum"
    )
    print(f"Fetching PubMed ESearch: {esearch_url}")
    resp = httpx2.get(esearch_url)
    resp.raise_for_status()
    esearch_bytes = resp.content
    (FIXTURES_DIR / "pubmed_esearch.xml").write_bytes(esearch_bytes)

    # Parse WebEnv and QueryKey
    root = ET.fromstring(esearch_bytes)
    web_env = root.findtext("WebEnv")
    query_key = root.findtext("QueryKey")
    assert web_env and query_key, "Failed to get history from ESearch"

    efetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
        f"db=pubmed&query_key={query_key}&WebEnv={web_env}&retstart=0&retmax=500&retmode=xml&tool=kittum"
    )
    print(f"Fetching PubMed EFetch: {efetch_url}")
    resp_fetch = httpx2.get(efetch_url)
    resp_fetch.raise_for_status()
    (FIXTURES_DIR / "pubmed_efetch.xml").write_bytes(resp_fetch.content)
    print(
        f"Saved pubmed_esearch.xml ({len(esearch_bytes)} bytes) and pubmed_efetch.xml ({len(resp_fetch.content)} bytes)"
    )


def record_pubmed_databank_only() -> None:
    # PMID 40701924 names NCT03052283 only in Article/DataBankList, not in its title or abstract.
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    esearch_url = f"{base}/esearch.fcgi?db=pubmed&term=40701924%5Bpmid%5D&usehistory=y&retmode=xml&tool=kittum"
    resp = httpx2.get(esearch_url)
    resp.raise_for_status()
    (FIXTURES_DIR / "pubmed_esearch_databank_only.xml").write_bytes(resp.content)
    root = ET.fromstring(resp.content)
    efetch_url = (
        f"{base}/efetch.fcgi?db=pubmed&query_key={root.findtext('QueryKey')}"
        f"&WebEnv={root.findtext('WebEnv')}&retstart=0&retmax=1&retmode=xml&tool=kittum"
    )
    resp_fetch = httpx2.get(efetch_url)
    resp_fetch.raise_for_status()
    (FIXTURES_DIR / "pubmed_efetch_databank_only.xml").write_bytes(resp_fetch.content)
    print(f"Saved the DataBankList fixtures (WebEnv={root.findtext('WebEnv')})")


def record_ctgov() -> None:
    # Nine trials, so two real pages of five; the bodies are saved byte for byte.
    page1_url = (
        "https://clinicaltrials.gov/api/v2/studies?"
        "query.term=lumacaftor%20AND%20AREA%5BPhase%5DPHASE2&pageSize=5&countTotal=true"
    )
    print(f"Fetching CT.gov page 1: {page1_url}")
    resp1 = httpx2.get(page1_url)
    resp1.raise_for_status()
    (FIXTURES_DIR / "ctgov_page1.json").write_bytes(resp1.content)

    next_token = json.loads(resp1.content).get("nextPageToken")
    assert next_token, "No nextPageToken on page 1"
    page2_url = f"{page1_url}&pageToken={next_token}"
    print(f"Fetching CT.gov page 2: {page2_url}")
    resp2 = httpx2.get(page2_url)
    resp2.raise_for_status()
    assert "nextPageToken" not in json.loads(resp2.content), "Expected two pages"
    (FIXTURES_DIR / "ctgov_page2.json").write_bytes(resp2.content)
    print(f"Saved ctgov_page1.json and ctgov_page2.json (next_token={next_token})")


# One review's searches on three sources (tests/corpus/test_duplicates.py and the workbench's
# duplicates journey): PubMed and Europe PMC find the same papers, and two of PubMed's papers name
# their ClinicalTrials.gov entries in their DataBankList.
DUPLICATES_DIR = FIXTURES_DIR / "duplicates"
DUPLICATES_PUBMED = (
    '"nicotine replacement"[tiab] AND "smoking cessation"[tiab] AND randomized controlled trial[pt]'
    " AND 2024/01/01:2024/03/31[dp]"
)
DUPLICATES_EUROPEPMC = (
    'TITLE_ABS:"nicotine replacement" AND TITLE_ABS:"smoking cessation"'
    ' AND PUB_TYPE:"Randomized Controlled Trial" AND FIRST_PDATE:[2024-01-01 TO 2024-03-31]'
)
DUPLICATES_CTGOV = "NCT03589989 OR NCT02564315"
DUPLICATES_CONTACT = "test@kittum.dev"  # the stack's contact address (apps/web/e2e/stack.ts)


def record_duplicates() -> None:
    """Run the three searches as Kittum does, through `RecordingTransport`, so the gateway replays
    them byte for byte. Europe PMC also runs with two records a page, to record its paging."""
    from kittum.gateway import HttpxTransport, RecordingTransport
    from kittum.gateway.testing import PassThroughGateway
    from kittum.search import CtgovSource, EuropePmcSource, PubMedSource, SearchPlan
    from kittum.storage import MemoryStore

    transport = RecordingTransport(
        HttpxTransport(contact_email=DUPLICATES_CONTACT),
        DUPLICATES_DIR,
        contact_email=DUPLICATES_CONTACT,
    )
    gateway = PassThroughGateway(transport)
    artifacts = MemoryStore()
    runs = [
        (PubMedSource(gateway, artifacts, contact_email=DUPLICATES_CONTACT), DUPLICATES_PUBMED),
        (EuropePmcSource(gateway, artifacts), DUPLICATES_EUROPEPMC),
        (EuropePmcSource(gateway, artifacts, page_size=2), DUPLICATES_EUROPEPMC),
        (CtgovSource(gateway, artifacts), DUPLICATES_CTGOV),
    ]
    for source, strategy in runs:
        plan = SearchPlan(topic_id="t", source=source.name, platform="", strategy=strategy)
        execution = source.run(plan)
        print(f"{source.name}: {execution.reported_count} reported, {execution.retrieved_count}")


if __name__ == "__main__":
    if sys.argv[1:] == ["duplicates"]:
        record_duplicates()
        sys.exit()
    record_pubmed()
    record_pubmed_databank_only()
    record_ctgov()
