"""Record the live responses that the enrichment tests replay.

Run once, by hand, with a database available (the enrichment writes rows):

    KITTUM_DATABASE_URL=... uv run python -m tests.enrich.record_fixtures

This is not a test. It talks to OpenAlex, Crossref, Europe PMC, Unpaywall, and PubTator 3.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import TYPE_CHECKING

import psycopg

from kittum.core import sha256_hex
from kittum.enrich import EnrichmentConfig, enrich
from kittum.gateway import HttpRequest, HttpResponse
from kittum.gateway.testing import PassThroughGateway
from tests.enrich.support import FIXTURES, config, doi_only_records, source_records

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Tx
    from kittum.gateway import Gateway
    from kittum.search import SourceRecord


class RecordingTransport:
    """Sends real requests and keeps each response under the URL that produced it."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._manifest: dict[str, str] = {}

    def send(self, request: HttpRequest) -> HttpResponse:
        http_request = urllib.request.Request(
            request.url, method=request.method, headers=dict(request.headers)
        )
        try:
            with urllib.request.urlopen(http_request, timeout=60) as response:
                status = response.status
                body = response.read()
                headers = tuple(response.headers.items())
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read()
            headers = ()
        filename = f"{sha256_hex(request.url.encode())[:16]}.json"
        (self._directory / filename).write_bytes(body)
        self._manifest[request.url] = filename
        return HttpResponse(status=status, headers=headers, body=body, body_hash=sha256_hex(body))

    def write_manifest(self) -> None:
        manifest = self._directory / "manifest.json"
        existing: dict[str, str] = json.loads(manifest.read_text()) if manifest.exists() else {}
        existing.update(self._manifest)
        manifest.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")


class DownSources:
    """A gateway where some sources are out of budget, for the fallback recordings."""

    def __init__(self, inner: PassThroughGateway, *sources: str) -> None:
        self._inner = inner
        self._sources = set(sources)

    def fetch(self, source: str, request: HttpRequest) -> HttpResponse:
        if source in self._sources:
            raise TimeoutError(f"{source} is out of budget")
        return self._inner.fetch(source, request)


def _run(
    records: tuple[SourceRecord, ...],
    gateway: Gateway,
    tx: Tx,
    config: EnrichmentConfig,
) -> None:
    enrich(tx, uuid.UUID(int=6), records, gateway, config=config)


def main() -> None:
    url = os.environ["KITTUM_DATABASE_URL"]
    transport = RecordingTransport(FIXTURES)
    gateway = PassThroughGateway(transport)
    openalex_down = DownSources(gateway, "openalex")
    metadata_down = DownSources(gateway, "openalex", "europepmc")
    with psycopg.connect(url) as connection, connection.transaction(force_rollback=True):
        connection.execute(
            "insert into project (id, name, policy_version) values (%s, 'wp06', '1')",
            (uuid.UUID(int=6),),
        )
        _run(source_records(), gateway, connection, config())
        _run(source_records(), openalex_down, connection, config())
        _run(doi_only_records(), gateway, connection, config())
        _run(doi_only_records(), metadata_down, connection, config())
    transport.write_manifest()
    print(f"recorded {len(transport._manifest)} responses in {FIXTURES}")


if __name__ == "__main__":
    main()
