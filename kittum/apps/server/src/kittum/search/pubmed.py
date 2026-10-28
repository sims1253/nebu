"""PubMed E-utilities source adapter (DESIGN.md, "Search"; docs/sources.md).

Supports ESearch with `usehistory=y`, date-window splitting when hits exceed 9,999,
and EFetch XML parsing into SourceRecords with structured abstracts and registry IDs.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import quote, urlencode

from kittum.core import Clock, Sha256, SystemClock, new_id
from kittum.gateway import HttpRequest
from kittum.search.models import SearchExecution, SearchPlan, SourceError, SourceRecord

if TYPE_CHECKING:
    from kittum.gateway import Gateway
    from kittum.storage import ArtifactStore

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DEFAULT_MAX_COUNT = 9999
# Date windows span every date PubMed can hold, so splitting never narrows the query.
EARLIEST_DATE = date(1000, 1, 1)
LATEST_DATE = date(3000, 12, 31)
BATCH_SIZE = 500

NCT_PATTERN = re.compile(r"(?i)\b(NCT\d{8})\b")
EUDRACT_PATTERN = re.compile(r"\b(\d{4}-\d{6}-\d{2})\b")
YEAR_PATTERN = re.compile(r"\b(19\d\d|20\d\d)\b")


# DataBankName values that NLM uses for clinical trial registries
# (https://www.nlm.nih.gov/bsd/medline_databank_source.html).
TRIAL_REGISTRY_DATABANKS = frozenset(
    {
        "ANZCTR",
        "ChiCTR",
        "ClinicalTrials.gov",
        "CRiS",
        "CTRI",
        "DRKS",
        "EudraCT",
        "IRCT",
        "ISRCTN",
        "JPRN",
        "NTR",
        "PACTR",
        "ReBec",
        "RPCEC",
        "SLCTR",
        "TCTR",
        "UMIN CTR",
    }
)


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
            if year_str and year_str.isdigit():
                year = int(year_str)

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

        # Registry IDs from DataBankList, which sits under Article. It also lists sequence and
        # data repositories (GenBank, PDB, ...), so keep only trial registries.
        for bank in medline.findall("Article/DataBankList/DataBank"):
            if (bank.findtext("DataBankName") or "").strip() not in TRIAL_REGISTRY_DATABANKS:
                continue
            for acc in bank.findall("AccessionNumberList/AccessionNumber"):
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


class ESearchResult(NamedTuple):
    count: int
    web_env: str
    query_key: str
    query_translation: str | None


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

    def _url(self, endpoint: str, params: dict[str, str]) -> str:
        params = {**params, "tool": self._tool}
        if self._contact_email:
            params["email"] = self._contact_email
        if self._ncbi_api_key:
            params["api_key"] = self._ncbi_api_key
        return f"{EUTILS_BASE}/{endpoint}?{urlencode(params, quote_via=quote)}"

    def _fetch(self, url: str, raw_hashes: list[Sha256]) -> tuple[bytes, Sha256]:
        """Fetch through the gateway, store the body, and record its hash."""
        resp = self._gateway.fetch("pubmed", HttpRequest(url=url))
        if not 200 <= resp.status < 300:
            raise SourceError(f"PubMed returned HTTP {resp.status} for {url}")
        body_hash = self._artifacts.put(resp.body)
        raw_hashes.append(body_hash)
        return resp.body, body_hash

    def _esearch(
        self,
        term: str,
        raw_hashes: list[Sha256],
        *,
        mindate: str | None = None,
        maxdate: str | None = None,
    ) -> ESearchResult:
        params = {"db": "pubmed", "term": term, "usehistory": "y", "retmode": "xml"}
        if mindate and maxdate:
            params |= {"datetype": "pdat", "mindate": mindate, "maxdate": maxdate}
        body, _ = self._fetch(self._url("esearch.fcgi", params), raw_hashes)

        root = ET.fromstring(body)
        error = root.findtext("ERROR")
        count_str = root.findtext("Count")
        web_env = root.findtext("WebEnv")
        query_key = root.findtext("QueryKey")
        if error or count_str is None or not web_env or not query_key:
            raise SourceError(f"PubMed ESearch failed for {term!r}: {error or 'incomplete reply'}")
        return ESearchResult(int(count_str), web_env, query_key, root.findtext("QueryTranslation"))

    def _split_date_windows(
        self,
        term: str,
        start_date: date,
        end_date: date,
        raw_hashes: list[Sha256],
    ) -> list[tuple[date, date, ESearchResult]]:
        """Recursively split a date range into subwindows with count <= max_count."""
        result = self._esearch(
            term,
            raw_hashes,
            mindate=start_date.strftime("%Y/%m/%d"),
            maxdate=end_date.strftime("%Y/%m/%d"),
        )

        if result.count == 0:
            return []

        if result.count <= self._max_count_per_window:
            logger.info(
                "PubMed date window %s to %s: count=%d",
                start_date.isoformat(),
                end_date.isoformat(),
                result.count,
            )
            return [(start_date, end_date, result)]

        if start_date >= end_date:
            # EFetch cannot page past the cap, so the rest of this day would be lost.
            raise SourceError(
                f"PubMed returns {result.count} records for {start_date.isoformat()} alone, "
                f"more than the {self._max_count_per_window} one window can fetch"
            )

        mid_date = start_date + (end_date - start_date) // 2
        left = self._split_date_windows(term, start_date, mid_date, raw_hashes)
        right = self._split_date_windows(term, mid_date + timedelta(days=1), end_date, raw_hashes)
        return left + right

    def run(self, plan: SearchPlan) -> SearchExecution:
        started_at = self._clock.now()
        raw_hashes: list[Sha256] = []

        initial = self._esearch(plan.strategy, raw_hashes)
        windows = [initial] if initial.count > 0 else []
        if initial.count > self._max_count_per_window:
            logger.info(
                "Search count %d exceeds %d; splitting into date windows",
                initial.count,
                self._max_count_per_window,
            )
            split = self._split_date_windows(plan.strategy, EARLIEST_DATE, LATEST_DATE, raw_hashes)
            windows = [result for _, _, result in split]
            # A record with distinct electronic and print dates can sit in two windows, so the sum
            # may exceed the original count, but it can never fall short of it.
            if sum(w.count for w in windows) < initial.count:
                raise SourceError(
                    f"PubMed date windows hold {sum(w.count for w in windows)} records, "
                    f"fewer than the {initial.count} the search reported"
                )

        records_by_pmid: dict[str, SourceRecord] = {}
        book_pmids: dict[str, None] = {}
        for window in windows:
            retstart = 0
            while retstart < window.count:
                retmax = min(BATCH_SIZE, window.count - retstart)
                params = {
                    "db": "pubmed",
                    "query_key": window.query_key,
                    "WebEnv": window.web_env,
                    "retstart": str(retstart),
                    "retmax": str(retmax),
                    "retmode": "xml",
                }
                body, body_hash = self._fetch(self._url("efetch.fcgi", params), raw_hashes)
                root = ET.fromstring(body)
                error = root.findtext("ERROR")
                if error:
                    raise SourceError(f"PubMed EFetch failed at retstart={retstart}: {error}")
                for record in parse_pubmed_xml(body, body_hash):
                    records_by_pmid.setdefault(record.provider_record_id, record)
                for book in root.findall("PubmedBookArticle"):
                    book_pmids[book.findtext("BookDocument/PMID") or ""] = None
                retstart += retmax

        # Keep the first occurrence of each PMID; ordinals follow that order.
        all_records = [
            r.model_copy(update={"ordinal": i}) for i, r in enumerate(records_by_pmid.values())
        ]
        skipped = tuple(pmid for pmid in book_pmids if pmid not in records_by_pmid)
        if len(all_records) + len(skipped) != initial.count:
            raise SourceError(
                f"PubMed reported {initial.count} records but returned {len(all_records)} "
                f"unique articles and {len(skipped)} skipped book records"
            )

        return SearchExecution(
            id=new_id(),
            plan=plan,
            kind="live_api",
            started_at=started_at,
            ended_at=self._clock.now(),
            query_translation=initial.query_translation,
            reported_count=initial.count,
            retrieved_count=len(all_records),
            raw_artifact_hashes=tuple(raw_hashes),
            records=tuple(all_records),
            skipped_record_ids=skipped,
        )
