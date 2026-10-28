"""A transport that answers from a script or a table, for tests of anything that talks HTTP."""

from typing import TYPE_CHECKING

from kittum.core import sha256_hex
from kittum.gateway import HttpRequest, HttpResponse, Transport

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


def response(
    status: int = 200,
    body: bytes = b"",
    headers: tuple[tuple[str, str], ...] = (),
) -> HttpResponse:
    """An `HttpResponse` for a `FakeTransport` script."""
    return HttpResponse(status=status, headers=headers, body=body, body_hash=sha256_hex(body))


class FakeTransport:
    """Answers in order from `sequence`, then by URL from `responses`.

    Without a `sequence` it behaves as before: one status and header set per URL. A `sequence`
    lets a test script a 429 followed by a 200, or five failures followed by a success.
    """

    def __init__(
        self,
        responses: Mapping[str, bytes] | None = None,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
        sequence: Iterable[HttpResponse] = (),
    ) -> None:
        self.responses = dict(responses or {})
        self.status = status
        self.headers = headers
        self.sequence = list(sequence)
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        if self.sequence:
            return self.sequence.pop(0)
        body = self.responses.get(request.url)
        if body is None:
            return response(404)
        return response(self.status, body, self.headers)


class PassThroughGateway:
    """A gateway with no limits, cache, or retries. Lets modules run before WP-04 lands."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def fetch(self, source: str, request: HttpRequest, *, use_cache: bool = True) -> HttpResponse:
        return self._transport.send(request)


__all__ = ["FakeTransport", "PassThroughGateway", "response"]
