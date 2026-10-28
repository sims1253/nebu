"""Values and seams that cross the gateway boundary."""

from typing import Protocol

from kittum.core import Record, Sha256


class SourcePolicy(Record):
    """What one source allows: its rate, concurrency, daily budget, and terms."""

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
    def fetch(
        self, source: str, request: HttpRequest, *, use_cache: bool = True
    ) -> HttpResponse: ...


__all__ = ["Gateway", "HttpRequest", "HttpResponse", "SourcePolicy", "Transport"]
