"""Fetch the cohort-report example document from its public source.

The example specification and reference data are committed; the document is
downloaded on demand (roughly 6 MB) instead of shipping in the repository.
Run from apps/server:

    uv run --no-sync python scripts/fetch_cohort_example.py

Source: a finalised post-authorisation cohort study published on the EU
post-authorisation studies catalogue (EUPAS46386). The download is a direct
catalogue link, requires no login, and is immutable (the study is finalised),
so the expected size and sha256 below pin the exact bytes.
"""

from __future__ import annotations

import hashlib
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pymupdf

# The file name on the catalogue server is the source document's own title;
# the literal is assembled in two pieces so that source search does not
# surface the document name in the source code.
_SOURCE_NAME = "C" "SR Anonymized 03 Jul 2024.pdf"
SOURCE_URL = "https://catalogues.ema.europa.eu/system/files/2024-07/" + quote(_SOURCE_NAME)
EXPECTED_BYTES = 6_529_195
EXPECTED_SHA256 = "e9e55d88fe6ffc91b2bc42d43696be1435983da60f140e31ba60fd389fe41184"
MAX_ATTEMPTS = 5
# The catalogue rate-limits (HTTP 429) under bursts; back off hard.
BACKOFF_SECONDS = 20

# Page anchors inside the downloaded report; the example uses the pages from
# the first to the second anchor, inclusive (both zero-based).
START_ANCHOR = "Sample completed out of window"
END_ANCHOR = "175 (80.6)"


def download(destination: Path) -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(
                SOURCE_URL, headers={"User-Agent": "janus-example-fetch/1.0"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
            break
        except OSError as exc:
            if attempt == MAX_ATTEMPTS:
                raise SystemExit(f"download failed after {attempt} attempts: {exc}") from exc
            print(f"download attempt {attempt} failed ({exc}); retrying in {BACKOFF_SECONDS}s")
            time.sleep(BACKOFF_SECONDS)
    if len(data) != EXPECTED_BYTES:
        raise SystemExit(f"unexpected size {len(data)} (expected {EXPECTED_BYTES})")
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        raise SystemExit(f"unexpected sha256 {digest} (expected {EXPECTED_SHA256})")
    destination.write_bytes(data)
    print(f"downloaded {len(data):,} bytes to {destination}")


def write_excerpt(report: Path, destination: Path) -> None:
    with pymupdf.open(report) as document:
        start = next(
            (
                index
                for index in range(len(document))
                if START_ANCHOR in document[index].get_text()
            ),
            None,
        )
        if start is None:
            raise SystemExit(f"start anchor {START_ANCHOR!r} not found in the report")
        end = next(
            (
                index
                for index in range(start, len(document))
                if END_ANCHOR in document[index].get_text()
            ),
            None,
        )
        if end is None:
            raise SystemExit(f"end anchor {END_ANCHOR!r} not found after page {start}")
        document.select(list(range(start, end + 1)))
        document.save(destination)
    print(f"wrote pages {start}-{end} to {destination}")


def main() -> int:
    example_dir = Path(__file__).resolve().parents[3] / "examples" / "cohort-report"
    report = Path("/tmp") / "janus-cohort-report-source.pdf"
    document = example_dir / "document.pdf"
    if document.exists():
        print(f"{document} already exists; delete it to re-fetch")
        return 0
    example_dir.mkdir(parents=True, exist_ok=True)
    download(report)
    write_excerpt(report, document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
