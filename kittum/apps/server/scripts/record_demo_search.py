"""Records the PubMed search behind `kittum demo seed`.

Run once from kittum/apps/server: `uv run python scripts/record_demo_search.py`. It runs the
demo strategy against NCBI E-utilities and writes the ESearch and EFetch replies to
src/kittum/app/demo_data/. The demo and the tests replay them; they never call NCBI.
"""

import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

from kittum.app.demo import PUBMED_STRATEGY

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OUT = Path(__file__).parents[1] / "src" / "kittum" / "app" / "demo_data"


def get(endpoint: str, params: dict[str, str]) -> bytes:
    url = f"{BASE}/{endpoint}?{urlencode({**params, 'tool': 'kittum'}, quote_via=quote)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def main() -> None:
    import xml.etree.ElementTree as ET

    esearch = get(
        "esearch.fcgi",
        {"db": "pubmed", "term": PUBMED_STRATEGY, "usehistory": "y", "retmode": "xml"},
    )
    root = ET.fromstring(esearch)
    count = root.findtext("Count") or "0"
    time.sleep(1)
    efetch = get(
        "efetch.fcgi",
        {
            "db": "pubmed",
            "query_key": root.findtext("QueryKey") or "1",
            "WebEnv": root.findtext("WebEnv") or "",
            "retstart": "0",
            "retmax": count,
            "retmode": "xml",
        },
    )
    (OUT / "pubmed_esearch.xml").write_bytes(esearch)
    (OUT / "pubmed_efetch.xml").write_bytes(efetch)
    print(f"{count} records")


if __name__ == "__main__":
    main()
