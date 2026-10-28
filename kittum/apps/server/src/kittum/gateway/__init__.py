"""All outbound HTTP (docs/sources.md, "How Kittum spreads load").

Per-source token buckets persisted in Postgres, a safety margin under every published limit,
`Retry-After` handling, retries with jitter, and a content-addressed response cache. A rerun of a
frozen strategy makes no network calls.

Owner: WP-04. Shipped now: the types, the `Transport` seam, and a fake transport in
`kittum.gateway.testing`. Still to build: the gateway itself (pyrate-limiter with a Postgres
bucket, stamina retries, the cache) and an httpx transport.
"""

from typing import Protocol

from kittum.core import Record, Sha256


class SourcePolicy(Record):
    source: str
    requests_per_second: float
    concurrency: int
    daily_budget: int | None = None
    terms_url: str


class HttpRequest(Record):
    method: str = "GET"
    url: str
    params: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None


class HttpResponse(Record):
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    body_hash: Sha256
    from_cache: bool = False


class Transport(Protocol):
    """Sends one request. The seam between the gateway and the network."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


class Gateway(Protocol):
    def fetch(self, source: str, request: HttpRequest) -> HttpResponse: ...


__all__ = ["Gateway", "HttpRequest", "HttpResponse", "SourcePolicy", "Transport"]
