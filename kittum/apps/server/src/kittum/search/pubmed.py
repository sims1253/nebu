"""PubMed E-utilities source adapter (DESIGN.md, "Search"; docs/sources.md).

Supports ESearch with `usehistory=y`, date-window splitting when hits exceed 9,999,
and EFetch XML parsing into SourceRecords with structured abstracts and registry IDs.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import TYPE_CHECKING

from kittum.core import Clock, Sha256, SystemClock, new_id, sha256_hex
from kittum.gateway import HttpRequest
from kittum.search.models import SearchExecution, SearchPlan, SourceRecord

if TYPE_CHECKING:
    from kittum.gateway import Gateway
    from kittum.storage import ArtifactStore

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_MAX_COUNT = 9999
BATCH_SIZE = 500

NCT_PATTERN = re.compile(r"(?i)\b(NCT\d{8})\b")
EUDRACT_PATTERN = re.compile(r"\b(\d{4}-\d{6}-\d{2})\b")
YEAR_PATTERN = re.compile(r"\b(19\d\d|20\d\d)\b")


def parse_pubmed_xml(
    xml_bytes: bytes, raw_hash: Sha256, start_ordinal: int = 0
) -> list[SourceRecord]:
    """Parse PubMed EFetch XML into SourceRecord models."""
    root = ET.fromstring(xml_bytes)
    records: list[SourceRecord] = []
    ordinal = start_ordinal

    for article in root.findall("PubmedArticle"):
        medline = article.find("MedlineCitation")
        if medline is None:
            continue

        pmid = medline.findtext("PMID") or ""
        art_elem = medline.find("Article")

        title = ""
        abstract: str | None = None
        authors: list[str] = []
        year: int | None = None
        journal: str | None = None
        doi: str | None = None
        pub_types: list[str] = []
        registry_ids: list[str] = []

        if art_elem is not None:
            title_elem = art_elem.find("ArticleTitle")
            if title_elem is not None:
                title = "".join(title_elem.itertext()).strip()

            ab_elem = art_elem.find("Abstract")
            if ab_elem is not None:
                sections: list[str] = []
                for child in ab_elem.findall("AbstractText"):
                    label = child.get("Label") or child.get("NlmCategory")
                    text = "".join(child.itertext()).strip()
                    if label:
                        sections.append(f"{label}: {text}")
                    elif text:
                        sections.append(text)
                if sections:
                    abstract = "\n\n".join(sections)

            authors_elem = art_elem.find("AuthorList")
            if authors_elem is not None:
                for author in authors_elem.findall("Author"):
                    collective = author.findtext("CollectiveName")
                    if collective:
                        authors.append(collective.strip())
                        continue
                    last = author.findtext("LastName")
                    fore = author.findtext("ForeName")
                    initials = author.findtext("Initials")
                    if last and initials:
                        authors.append(f"{last} {initials}")
                    elif last and fore:
                        authors.append(f"{last}, {fore}")
                    elif last:
                        authors.append(last)

            # Year extraction
            year_str = art_elem.findtext("Journal/JournalIssue/PubDate/Year") or art_elem.findtext(
                "ArticleDate/Year"
            )
            if not year_str:
                medline_date = art_elem.findtext("Journal/JournalIssue/PubDate/MedlineDate")
                if medline_date:
                    m = YEAR_PATTERN.search(medline_date)
                    if m:
                        year_str = m.group(1)
            if year_str:
                try:
                    year = int(year_str)
                except ValueError:
                    year = None

            # Journal
            journal = (
                art_elem.findtext("Journal/Title")
                or art_elem.findtext("Journal/ISOAbbreviation")
                or medline.findtext("MedlineJournalInfo/MedlineTA")
            )

            # DOI
            for eloc in art_elem.findall("ELocationID"):
                if eloc.get("EIdType") == "doi" and eloc.text:
                    doi = eloc.text.strip()
                    break

            # Publication types
            pt_list = art_elem.find("PublicationTypeList")
            if pt_list is not None:
                pub_types = [
                    pt.text.strip() for pt in pt_list.findall("PublicationType") if pt.text
                ]

        # DOI from PubmedData if not yet found
        if not doi:
            for art_id in article.findall("PubmedData/ArticleIdList/ArticleId"):
                if art_id.get("IdType") == "doi" and art_id.text:
                    doi = art_id.text.strip()
                    break

        # Registry IDs from DataBankList
        databank_list = medline.find("DataBankList")
        if databank_list is not None:
            for db in databank_list.findall("DataBank"):
                for acc in db.findall("AccessionNumberList/AccessionNumber"):
                    if acc.text:
                        registry_ids.append(acc.text.strip())

        # Registry IDs from abstract and title
        combined_text = f"{title} {abstract or ''}"
        for nct in NCT_PATTERN.findall(combined_text):
            registry_ids.append(nct.upper())
        for eudra in EUDRACT_PATTERN.findall(combined_text):
            registry_ids.append(eudra)

        # Deduplicate registry_ids preserving order
        unique_reg_ids = tuple(dict.fromkeys(registry_ids))

        record = SourceRecord(
            occurrence_id=new_id(),
            source="pubmed",
            provider_record_id=pmid,
            ordinal=ordinal,
            title=title,
            abstract=abstract,
            authors=tuple(authors),
            year=year,
            journal=journal,
            doi=doi,
            pmid=pmid,
            registry_ids=unique_reg_ids,
            publication_types=tuple(pub_types),
            raw_hash=raw_hash,
        )
        records.append(record)
        ordinal += 1

    return records


class PubMedSource:
    """Live API source adapter for PubMed E-utilities."""

    name: str = "pubmed"

    def __init__(
        self,
        gateway: Gateway,
        artifacts: ArtifactStore,
        *,
        contact_email: str | None = None,
        ncbi_api_key: str | None = None,
        tool: str = "kittum",
        clock: Clock | None = None,
        max_count_per_window: int = DEFAULT_MAX_COUNT,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._contact_email = contact_email
        self._ncbi_api_key = ncbi_api_key
        self._tool = tool
        self._clock = clock or SystemClock()
        self._max_count_per_window = max_count_per_window

    def _auth_params(self) -> dict[str, str]:
        params: dict[str, str] = {"tool": self._tool}
        if self._contact_email:
            params["email"] = self._contact_email
        if self._ncbi_api_key:
            params["api_key"] = self._ncbi_api_key
        return params

    def _build_esearch_url(
        self,
        term: str,
        *,
        mindate: str | None = None,
        maxdate: str | None = None,
        datetype: str = "pdat",
    ) -> str:
        parts = [
            f"{EUTILS_BASE}/esearch.fcgi?db=pubmed",
            f"term={term}",
            "usehistory=y",
            "retmode=xml",
        ]
        auth = self._auth_params()
        for k, v in auth.items():
            parts.append(f"{k}={v}")
        if mindate and maxdate:
            parts.append(f"datetype={datetype}")
            parts.append(f"mindate={mindate}")
            parts.append(f"maxdate={maxdate}")
        return "&".join(parts)

    def _build_efetch_url(
        self,
        query_key: str,
        web_env: str,
        retstart: int,
        retmax: int,
    ) -> str:
        parts = [
            f"{EUTILS_BASE}/efetch.fcgi?db=pubmed",
            f"query_key={query_key}",
            f"WebEnv={web_env}",
            f"retstart={retstart}",
            f"retmax={retmax}",
            "retmode=xml",
        ]
        auth = self._auth_params()
        for k, v in auth.items():
            parts.append(f"{k}={v}")
        return "&".join(parts)

    def _esearch(
        self,
        term: str,
        *,
        mindate: str | None = None,
        maxdate: str | None = None,
    ) -> tuple[int, str | None, str | None, str | None, bytes, Sha256]:
        url = self._build_esearch_url(term, mindate=mindate, maxdate=maxdate)
        req = HttpRequest(url=url)
        resp = self._gateway.fetch("pubmed", req)
        body = resp.body
        body_hash = sha256_hex(body)
        self._artifacts.put(body)

        root = ET.fromstring(body)
        count_str = root.findtext("Count")
        count = int(count_str) if count_str else 0
        web_env = root.findtext("WebEnv")
        query_key = root.findtext("QueryKey")
        query_trans = root.findtext("QueryTranslation")
        return count, web_env, query_key, query_trans, body, body_hash

    def _split_date_windows(
        self,
        term: str,
        start_date: date,
        end_date: date,
    ) -> list[tuple[date, date, int, str, str, str | None, Sha256]]:
        """Recursively split a date range into subwindows with count <= max_count."""
        min_str = start_date.strftime("%Y/%m/%d")
        max_str = end_date.strftime("%Y/%m/%d")

        count, web_env, query_key, q_trans, _, b_hash = self._esearch(
            term, mindate=min_str, maxdate=max_str
        )

        if count == 0:
            return []

        if count <= self._max_count_per_window or start_date >= end_date:
            logger.info(
                "PubMed date window %s to %s: count=%d",
                start_date.isoformat(),
                end_date.isoformat(),
                count,
            )
            assert web_env and query_key
            return [(start_date, end_date, count, web_env, query_key, q_trans, b_hash)]

        # Split in half
        mid_date = start_date + (end_date - start_date) // 2
        left = self._split_date_windows(term, start_date, mid_date)
        right = self._split_date_windows(term, mid_date + timedelta(days=1), end_date)
        return left + right

    def run(self, plan: SearchPlan) -> SearchExecution:
        started_at = self._clock.now()
        exec_id = new_id()
        raw_hashes: list[Sha256] = []

        # Initial ESearch
        count, web_env, query_key, query_trans, _, initial_hash = self._esearch(plan.strategy)
        raw_hashes.append(initial_hash)

        windows: list[tuple[int, str, str]] = []
        if count > self._max_count_per_window:
            # Need date window splitting
            logger.info(
                "Search count %d exceeds %d; splitting into date windows",
                count,
                self._max_count_per_window,
            )
            start_date = date(1900, 1, 1)
            end_date = self._clock.now().date()
            split_windows = self._split_date_windows(plan.strategy, start_date, end_date)
            for _, _, win_count, win_env, win_key, _, win_hash in split_windows:
                windows.append((win_count, win_env, win_key))
                raw_hashes.append(win_hash)
        else:
            if count > 0:
                assert web_env and query_key
                windows.append((count, web_env, query_key))

        # EFetch records for all windows
        all_records: list[SourceRecord] = []
        current_ordinal = 0

        for win_count, w_env, w_key in windows:
            retstart = 0
            while retstart < win_count:
                retmax = min(BATCH_SIZE, win_count - retstart)
                fetch_url = self._build_efetch_url(w_key, w_env, retstart, retmax)
                req = HttpRequest(url=fetch_url)
                resp = self._gateway.fetch("pubmed", req)
                body = resp.body
                body_hash = sha256_hex(body)
                self._artifacts.put(body)
                raw_hashes.append(body_hash)

                records = parse_pubmed_xml(body, body_hash, start_ordinal=current_ordinal)
                all_records.extend(records)
                current_ordinal += len(records)
                retstart += retmax

        ended_at = self._clock.now()
        retrieved_count = len(all_records)
        reported_count = count

        return SearchExecution(
            id=exec_id,
            plan=plan,
            kind="live_api",
            started_at=started_at,
            ended_at=ended_at,
            query_translation=query_trans,
            reported_count=reported_count,
            retrieved_count=retrieved_count,
            raw_artifact_hashes=tuple(raw_hashes),
            records=tuple(all_records),
        )
