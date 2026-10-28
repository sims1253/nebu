"""Record the real case of issue #60, which `test_acquisition.py` replays from `fixtures/jmir/`.

Run once, by hand (no database needed):

    uv run python -m tests.documents.record_jmir /path/to/the/live/run/recording/sources

This is not a test. On 28 October 2026 a live run asked Unpaywall about the JMIR Formative Research
trial 10.2196/66709, and Unpaywall named the trial's CONSORT-EHEALTH checklist, its supplementary
file `formative_v9i1e66709_app2.pdf`, as the article's PDF; Kittum stored it as the trial's full
text. That run's recording holds Unpaywall's answer and the checklist, and both are copied from it
unchanged. PubMed Central's answer (ID converter, bucket listing, metadata, the article's PDF) and
Semantic Scholar's, which names the same checklist, are recorded live through the gateway's own
`RecordingTransport`. Semantic Scholar is asked only with a key: set
KITTUM_SEMANTIC_SCHOLAR_API_KEY. The key travels in a header, which no recording keeps.
"""

import json
import os
import shutil
import sys
from pathlib import Path

from kittum.enrich import EnrichmentConfig, WorkIds, lookup_full_text
from kittum.gateway import HttpRequest, HttpxTransport, RecordingTransport
from kittum.gateway.testing import PassThroughGateway
from tests.enrich.support import CONTACT_EMAIL

JMIR = Path(__file__).parent / "fixtures" / "jmir"
DOI = "10.2196/66709"
PMID = "40116747"
UNPAYWALL = "https://api.unpaywall.org/v2/10.2196%2F66709?email=contact%40example.invalid"
CHECKLIST = (
    "https://jmir.org/api/download?alt_name=formative_v9i1e66709_app2.pdf"
    "&filename=3bd69eb1-0665-11f0-9506-01e71748f354.pdf"
)
ARTICLE = "https://pmc-oa-opendata.s3.amazonaws.com/PMC11951811.1/PMC11951811.1.pdf"


def main(live_run: Path) -> None:
    JMIR.mkdir(exist_ok=True)
    recorded = json.loads((live_run / "manifest.json").read_text())
    transport = RecordingTransport(
        HttpxTransport(contact_email=CONTACT_EMAIL), JMIR, contact_email=CONTACT_EMAIL
    )
    gateway = PassThroughGateway(transport)
    config = EnrichmentConfig(
        contact_email=CONTACT_EMAIL,
        semantic_scholar_api_key=os.environ.get("KITTUM_SEMANTIC_SCHOLAR_API_KEY"),
    )
    for source in ("pmc", "semanticscholar"):
        lookup_full_text(gateway, source, WorkIds(doi=DOI, pmid=PMID), config=config)
    gateway.fetch("pmc", HttpRequest(url=ARTICLE))
    manifest = json.loads((JMIR / "manifest.json").read_text())
    for url, name in ((UNPAYWALL, "unpaywall-10.2196-66709.json"), (CHECKLIST, "app2.pdf")):
        (entry,) = recorded[url]
        shutil.copyfile(live_run / entry["file"], JMIR / name)
        manifest[url] = [entry | {"file": name}]
    (JMIR / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
