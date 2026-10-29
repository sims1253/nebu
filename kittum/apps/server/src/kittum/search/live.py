"""The live search sources by name, as the API and the CLI start them."""

from typing import TYPE_CHECKING

from kittum.search.ctgov import CtgovSource
from kittum.search.europepmc import EuropePmcSource
from kittum.search.pubmed import PubMedSource

if TYPE_CHECKING:
    from kittum.core import Clock
    from kittum.gateway import Gateway
    from kittum.search.models import Source
    from kittum.storage import ArtifactStore

LIVE_PLATFORMS = {
    "pubmed": "PubMed",
    "europepmc": "Europe PMC",
    "ctgov": "ClinicalTrials.gov",
}
"""Each live source's name and the platform its searches record."""

_ALIASES = {
    "pubmed": "pubmed",
    "medline": "pubmed",
    "europepmc": "europepmc",
    "europe_pmc": "europepmc",
    "epmc": "europepmc",
    "ctgov": "ctgov",
    "clinicaltrials": "ctgov",
}


def live_source(
    name: str,
    gateway: Gateway,
    artifacts: ArtifactStore,
    *,
    contact_email: str | None = None,
    ncbi_api_key: str | None = None,
    clock: Clock | None = None,
) -> Source | None:
    """The live source a plan's `source` names, or None when Kittum can only import its exports."""
    match _ALIASES.get(name.strip().lower()):
        case "pubmed":
            return PubMedSource(
                gateway,
                artifacts,
                contact_email=contact_email,
                ncbi_api_key=ncbi_api_key,
                clock=clock,
            )
        case "europepmc":
            return EuropePmcSource(gateway, artifacts, clock=clock)
        case "ctgov":
            return CtgovSource(gateway, artifacts, clock=clock)
        case _:
            return None
