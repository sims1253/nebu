"""Values and seams that cross the gateway boundary."""

from typing import Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

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


def effective_request(request: HttpRequest) -> HttpRequest:
    """Merge a query already in `url` into `params`, where httpx keeps it.

    httpx drops a URL's own query as soon as `params` is passed, even an empty list. Sending and
    hashing both use this form, so they always mean the same request.
    """
    split = urlsplit(request.url)
    if not split.query:
        return request
    params = tuple(parse_qsl(split.query, keep_blank_values=True)) + tuple(request.params)
    url = urlunsplit((split.scheme, split.netloc, split.path, "", split.fragment))
    return HttpRequest(
        method=request.method,
        url=url,
        params=params,
        headers=request.headers,
        body=request.body,
    )


class Gateway(Protocol):
    def fetch(
        self, source: str, request: HttpRequest, *, use_cache: bool = True
    ) -> HttpResponse: ...


__all__ = ["Gateway", "HttpRequest", "HttpResponse", "SourcePolicy", "Transport"]
