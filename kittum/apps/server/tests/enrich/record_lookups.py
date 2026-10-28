"""Record the live responses that `test_full_text_lookups.py` replays from `fixtures/lookups/`.

Run once, by hand (no database needed):

    uv run python -m tests.enrich.record_lookups /path/to/the/live/run/recording/sources

This is not a test. It asks medRxiv and bioRxiv about one preprint each, arXiv about one paper,
and Europe PMC about the 24 PMIDs and 24 DOIs of the 28 October 2026 live run, all through the
gateway's own `RecordingTransport`. Europe PMC's answer to that run, a 503 during its outage, is
copied from the run's recording, so the replay fails as the run did, then gets today's answer.
CORE is not recorded: it answers only with a key, and no key may reach a recording.
"""

import json
import shutil
import sys
from pathlib import Path

from kittum.enrich import EnrichmentConfig, WorkIds, europepmc, lookup_full_text
from kittum.gateway import HttpxTransport, RecordingTransport
from kittum.gateway.testing import PassThroughGateway
from tests.enrich.support import CONTACT_EMAIL, FIXTURES

LOOKUPS = FIXTURES / "lookups"
MEDRXIV = "10.1101/2020.03.19.20039131"
BIORXIV = "10.1101/2023.12.01.569634"
ARXIV = "10.48550/arXiv.2101.00001"
LIVE_RUN_PMIDS = [
    "40322715",
    "40183092",
    "40148133",
    "40116747",
    "40106803",
    "40071276",
    "40004917",
    "40002306",
    "39987579",
    "39978139",
    "39977412",
    "39931130",
    "39924464",
    "39923892",
    "39914192",
    "39804633",
    "39665734",
    "39659160",
    "39477130",
    "39300203",
    "39271186",
    "39233579",
    "39134144",
    "38700502",
]


def live_run_dois(live_run: Path) -> list[str]:
    """The DOIs the live run asked Europe PMC about, in its order."""
    from urllib.parse import parse_qs, urlsplit

    recorded = json.loads((live_run / "manifest.json").read_text())
    url = next(u for u in recorded if "europepmc/webservices/rest/search" in u)
    query = parse_qs(urlsplit(url).query)["query"][0]
    return [t.removeprefix('DOI:"').removesuffix('"') for t in query.split(" OR ") if "DOI" in t]


def main(live_run: Path) -> None:
    LOOKUPS.mkdir(exist_ok=True)
    transport = RecordingTransport(
        HttpxTransport(contact_email=CONTACT_EMAIL), LOOKUPS, contact_email=CONTACT_EMAIL
    )
    gateway = PassThroughGateway(transport)
    config = EnrichmentConfig(contact_email=CONTACT_EMAIL)
    for doi in (MEDRXIV, BIORXIV):
        lookup_full_text(gateway, "biorxiv", WorkIds(doi=doi), config=config)
    lookup_full_text(gateway, "arxiv", WorkIds(doi=ARXIV), config=config)
    dois = live_run_dois(live_run)
    europepmc.works(gateway, pmids=LIVE_RUN_PMIDS, dois=dois, batch_size=100)
    manifest = json.loads((LOOKUPS / "manifest.json").read_text())
    (url,) = [u for u in manifest if "europepmc" in u]
    recorded = json.loads((live_run / "manifest.json").read_text())
    outage = next(v for u, v in recorded.items() if "europepmc/webservices/rest/search" in u)
    shutil.copyfile(live_run / outage[0]["file"], LOOKUPS / "europepmc-503.json")
    manifest[url] = [e | {"file": "europepmc-503.json"} for e in outage] + manifest[url]
    (LOOKUPS / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
