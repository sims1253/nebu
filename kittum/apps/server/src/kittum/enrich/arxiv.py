"""arXiv: a preprint's PDF and licence, by its arXiv DOI (10.48550/arXiv.<id>).

The OAI-PMH record states the licence, which the search API does not. PDFs come from
`export.arxiv.org`, the host arXiv asks automated clients to use. See docs/sources.md.
"""

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from kittum.core import canonical_doi
from kittum.enrich._http import fetch
from kittum.enrich.types import FullTextLookup, OaLocation, WorkIds

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "arxiv"
OAI = "https://oaipmh.arxiv.org/oai"
PDF = "https://export.arxiv.org/pdf"
PREFIX = "10.48550/arxiv."

_LICENCE = re.compile(rb"<license>([^<]+)</license>")
_MISSING = re.compile(rb'<error code="idDoesNotExist"')


def arxiv_id(ids: WorkIds) -> str | None:
    doi = canonical_doi(ids.doi)
    return doi[len(PREFIX) :] if doi and doi.startswith(PREFIX) else None


def find(gateway: Gateway, ids: WorkIds) -> FullTextLookup | None:
    found = arxiv_id(ids)
    if found is None:
        return None
    params = {
        "verb": "GetRecord",
        "identifier": f"oai:arXiv.org:{found}",
        "metadataPrefix": "arXiv",
    }
    url = f"{OAI}?{urlencode(params)}"
    body = fetch(gateway, SOURCE, url, accept="application/xml")
    if body is None or _MISSING.search(body):
        return FullTextLookup(source=SOURCE, url=url)
    licence = _LICENCE.search(body)
    return FullTextLookup(
        source=SOURCE,
        url=url,
        locations=(
            OaLocation(
                url=f"{PDF}/{found}",
                licence=licence.group(1).decode().strip() if licence else None,
                version="submitted",
                source=SOURCE,
                format="pdf",
            ),
        ),
    )
