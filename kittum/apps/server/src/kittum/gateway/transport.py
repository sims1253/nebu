"""The real transport: httpx2, one connection pool, a polite User-Agent."""

import importlib.metadata

import httpx2

from kittum.core import sha256_hex
from kittum.gateway.types import HttpRequest, HttpResponse, effective_request


def _version() -> str:
    try:
        return importlib.metadata.version("kittum-server")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - installed in CI
        return "0.0.0"


def user_agent(contact_email: str | None = None) -> str:
    """The value sent in `User-Agent`, naming the version and a contact."""
    agent = f"Kittum/{_version()}"
    if contact_email:
        agent += f" (+mailto:{contact_email})"
    return agent


class HttpxTransport:
    """Sends one request per call over a shared client with a fixed timeout."""

    def __init__(
        self,
        *,
        contact_email: str | None = None,
        timeout: float = 30.0,
        client: httpx2.Client | None = None,
    ) -> None:
        self.user_agent = user_agent(contact_email)
        self._client = client or httpx2.Client(
            timeout=httpx2.Timeout(timeout), follow_redirects=True
        )

    def send(self, request: HttpRequest) -> HttpResponse:
        request = effective_request(request)
        headers = dict(request.headers)
        headers.setdefault("User-Agent", self.user_agent)
        response = self._client.request(
            request.method,
            request.url,
            params=list(request.params),
            headers=headers,
            content=request.body,
        )
        body = response.content
        return HttpResponse(
            status=response.status_code,
            headers=tuple(response.headers.multi_items()),
            body=body,
            body_hash=sha256_hex(body),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpxTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


__all__ = ["HttpxTransport", "user_agent"]
