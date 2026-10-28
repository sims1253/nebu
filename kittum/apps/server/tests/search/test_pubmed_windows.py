from datetime import date
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest

from kittum.core import sha256_hex
from kittum.gateway import HttpRequest, HttpResponse
from kittum.gateway.testing import PassThroughGateway
from kittum.ledger.testing import InMemoryLedger
from kittum.search import PubMedSource, SearchPlan, SourceError, get_execution, run
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx

PLAN = SearchPlan(topic_id="t", source="pubmed", platform="PubMed", strategy="lumacaftor")

# PMID -> its [dp] dates (electronic and print) and whether EFetch returns it as a book.
RECORDS: dict[str, tuple[tuple[date, ...], bool]] = {
    "1": ((date(1850, 6, 1),), False),  # before 1900
    "2": ((date(2030, 1, 1),), False),  # print date in the future
    "3": ((date(2019, 12, 20), date(2020, 2, 1)), False),  # epub and print in two windows
    "4": ((date(2020, 1, 15),), False),
    "5": ((date(2020, 1, 16),), False),
}


class DatedPubMed:
    """Answers ESearch and EFetch from `records`, as PubMed does for `[dp]` date windows."""

    def __init__(
        self, records: dict[str, tuple[tuple[date, ...], bool]], *, reported_extra: int = 0
    ) -> None:
        self.records = records
        self.reported_extra = reported_extra
        self.fetched: list[str] = []
        self.windows: list[tuple[str, str]] = []

    def _matches(self, mindate: str | None, maxdate: str | None) -> list[str]:
        if mindate is None or maxdate is None:
            return sorted(self.records, key=int)
        lo, hi = (date(*map(int, d.split("/"))) for d in (mindate, maxdate))
        return [
            pmid
            for pmid, (dates, _) in sorted(self.records.items(), key=lambda kv: int(kv[0]))
            if any(lo <= d <= hi for d in dates)
        ]

    def send(self, request: HttpRequest) -> HttpResponse:
        url = urlparse(request.url)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.endswith("esearch.fcgi"):
            mindate, maxdate = q.get("mindate"), q.get("maxdate")
            count = len(self._matches(mindate, maxdate))
            if mindate is None:
                count += self.reported_extra
            else:
                self.windows.append((mindate, maxdate or ""))
            body = (
                f"<eSearchResult><Count>{count}</Count><QueryKey>1</QueryKey>"
                f"<WebEnv>{mindate}|{maxdate}</WebEnv></eSearchResult>"
            ).encode()
        else:
            mindate, maxdate = q["WebEnv"].split("|")
            pmids = self._matches(None if mindate == "None" else mindate, maxdate)
            start = int(q["retstart"])
            batch = pmids[start : start + int(q["retmax"])]
            self.fetched.extend(batch)
            body = (
                b"<PubmedArticleSet>"
                + b"".join(self._xml(p) for p in batch)
                + b"</PubmedArticleSet>"
            )
        return HttpResponse(status=200, headers=(), body=body, body_hash=sha256_hex(body))

    def _xml(self, pmid: str) -> bytes:
        if self.records[pmid][1]:
            return f"<PubmedBookArticle><BookDocument><PMID>{pmid}</PMID></BookDocument></PubmedBookArticle>".encode()
        return (
            f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID>"
            f"<Article><ArticleTitle>Record {pmid}</ArticleTitle></Article>"
            f"</MedlineCitation></PubmedArticle>"
        ).encode()


def _source(pubmed: DatedPubMed, artifacts: MemoryStore) -> PubMedSource:
    return PubMedSource(PassThroughGateway(pubmed), artifacts, max_count_per_window=2)


def test_split_search_keeps_every_record_once_whatever_its_date() -> None:
    pubmed = DatedPubMed(RECORDS)
    artifacts = MemoryStore()

    execution = _source(pubmed, artifacts).run(PLAN)

    # The windows run from the earliest to the latest date PubMed can hold.
    assert pubmed.windows[0][0] == "1000/01/01"
    assert max(hi for _, hi in pubmed.windows) == "3000/12/31"
    # Record 3 came back in two windows and is kept once, in first-seen order.
    assert pubmed.fetched.count("3") == 2
    assert sorted(r.pmid or "" for r in execution.records) == ["1", "2", "3", "4", "5"]
    assert [r.ordinal for r in execution.records] == [0, 1, 2, 3, 4]
    assert execution.reported_count == execution.retrieved_count == 5
    assert execution.skipped_record_ids == ()


def test_split_search_fails_when_windows_cannot_cover_the_reported_count() -> None:
    # The unrestricted search reports a record that no date window finds.
    single_dated = {pmid: RECORDS[pmid] for pmid in ("1", "2", "4", "5")}
    pubmed = DatedPubMed(single_dated, reported_extra=1)

    with pytest.raises(SourceError, match="date windows hold 4 records, fewer than the 5"):
        _source(pubmed, MemoryStore()).run(PLAN)


def test_search_fails_when_unique_records_fall_short_of_the_count() -> None:
    # Under the cap there is no split; one reported record never arrives from EFetch.
    pubmed = DatedPubMed({"4": RECORDS["4"]}, reported_extra=1)
    source = PubMedSource(PassThroughGateway(pubmed), MemoryStore())

    with pytest.raises(SourceError, match="reported 2 records but returned 1 unique articles"):
        source.run(PLAN)


@pytest.mark.postgres
def test_skipped_book_records_are_reported_and_stored(
    pg: Tx, project_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    pubmed = DatedPubMed({**RECORDS, "6": ((date(2021, 3, 1),), True)})
    artifacts = MemoryStore()
    ledger = InMemoryLedger()

    execution = run(
        pg,
        project_id,
        PLAN,
        _source(pubmed, artifacts),
        ledger=ledger,
        actor_id=actor_id,
        artifacts=artifacts,
    )

    assert execution.reported_count == 6
    assert execution.retrieved_count == 5
    assert execution.skipped_record_ids == ("6",)
    loaded = get_execution(pg, project_id, execution.id)
    assert loaded is not None
    assert loaded.skipped_record_ids == ("6",)
    assert len(loaded.records) == 5
    (event,) = ledger.events(pg, project_id)
    assert event.payload["skipped_record_ids"] == ["6"]
