import logging
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from kittum.core import FixedClock, SystemClock, sha256_hex
from kittum.gateway import HttpRequest, HttpResponse
from kittum.search import PubMedSource, SourceError
from kittum.storage import MemoryStore


class SimulatedLargeCountTransport:
    """Simulates PubMed count responses for date window splitting.

    If date window span is > 100 days, returns count 15,000 (> 9,999).
    If date window span <= 100 days, returns count 500.
    """

    def __init__(self, small_window_count: int = 450) -> None:
        self.small_window_count = small_window_count
        self.requested_windows: list[tuple[date, date, int]] = []
        self.esearch_calls = 0

    def send(self, request: HttpRequest) -> HttpResponse:
        parsed = urlparse(request.url)
        params = parse_qs(parsed.query)

        if "esearch.fcgi" in parsed.path:
            self.esearch_calls += 1
            mindate_str = params.get("mindate", [None])[0]
            maxdate_str = params.get("maxdate", [None])[0]

            if not mindate_str or not maxdate_str:
                # Top-level query without dates: simulate large count
                count = 25000
                web_env = "MCID_root"
                query_key = "1"
            else:
                s_y, s_m, s_d = [int(p) for p in mindate_str.split("/")]
                e_y, e_m, e_d = [int(p) for p in maxdate_str.split("/")]
                s_date = date(s_y, s_m, s_d)
                e_date = date(e_y, e_m, e_d)
                span = (e_date - s_date).days

                count = 15000 if span > 100 else self.small_window_count

                web_env = f"MCID_{s_date}_{e_date}"
                query_key = "1"
                if count <= 9999:
                    self.requested_windows.append((s_date, e_date, count))

            xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
            <eSearchResult>
              <Count>{count}</Count>
              <RetMax>0</RetMax>
              <RetStart>0</RetStart>
              <QueryKey>{query_key}</QueryKey>
              <WebEnv>{web_env}</WebEnv>
              <QueryTranslation>"lumacaftor"[All Fields]</QueryTranslation>
            </eSearchResult>
            """.encode()
            return HttpResponse(status=200, headers=(), body=xml, body_hash=sha256_hex(xml))

        if "efetch.fcgi" in parsed.path:
            # Minimal empty article set for mock fetch
            xml = b"""<?xml version="1.0" encoding="UTF-8" ?>
            <PubmedArticleSet>
            </PubmedArticleSet>"""
            return HttpResponse(status=200, headers=(), body=xml, body_hash=sha256_hex(xml))

        return HttpResponse(status=404, headers=(), body=b"", body_hash=sha256_hex(b""))


class SimulatedGateway:
    def __init__(self, transport: SimulatedLargeCountTransport) -> None:
        self._transport = transport

    def fetch(self, _source: str, request: HttpRequest) -> HttpResponse:
        return self._transport.send(request)


def test_date_window_splitting_produces_complete_non_overlapping_windows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = SimulatedLargeCountTransport()
    gateway = SimulatedGateway(transport)
    artifacts = MemoryStore()

    # Fixed clock to 2020-12-31 so the date range is fixed (1900-01-01 to 2020-12-31)
    clock = FixedClock(SystemClock().now().replace(year=2020, month=12, day=31))
    source = PubMedSource(
        gateway,  # type: ignore[arg-type]
        artifacts,
        clock=clock,
        max_count_per_window=9999,
    )

    start_date = date(2020, 1, 1)
    end_date = date(2020, 12, 31)

    raw_hashes: list[str] = []
    with caplog.at_level(logging.INFO):
        windows = source._split_date_windows("lumacaftor", start_date, end_date, raw_hashes)

    # 1. Non-empty list of windows
    assert len(windows) > 1

    # 2. Every window has count <= 9,999
    for w_start, w_end, result in windows:
        assert 0 < result.count <= 9999
        assert w_start <= w_end

    # Every ESearch reply is a raw response of the execution, including the ones that were split
    assert len(raw_hashes) == transport.esearch_calls > len(windows)
    assert all(artifacts.exists(h) for h in raw_hashes)

    # 3. Check complete coverage from start_date to end_date
    assert windows[0][0] == start_date
    assert windows[-1][1] == end_date

    # 4. Check non-overlapping and contiguous (no gaps)
    for i in range(len(windows) - 1):
        curr_end = windows[i][1]
        next_start = windows[i + 1][0]
        assert next_start == curr_end + timedelta(days=1), (
            f"Gap or overlap between window {windows[i]} and {windows[i + 1]}"
        )

    # 5. Check logging of each window
    log_records = [rec for rec in caplog.records if "PubMed date window" in rec.message]
    assert len(log_records) == len(windows)
    for r in log_records:
        assert "count=" in r.message


def test_a_single_day_over_the_cap_fails_instead_of_truncating() -> None:
    # Every window, down to one day, reports more records than EFetch can page through.
    transport = SimulatedLargeCountTransport(small_window_count=15000)
    source = PubMedSource(SimulatedGateway(transport), MemoryStore())  # type: ignore[arg-type]

    with pytest.raises(SourceError, match="for 2020-01-01 alone"):
        source._split_date_windows("lumacaftor", date(2020, 1, 1), date(2020, 1, 4), [])
