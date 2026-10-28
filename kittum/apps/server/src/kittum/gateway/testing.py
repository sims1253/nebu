"""A transport that answers from a table, for tests of anything that talks HTTP."""

from kittum.core import sha256_hex
from kittum.gateway import HttpRequest, HttpResponse


class FakeTransport:
    def __init__(self, responses: dict[str, bytes], status: int = 200) -> None:
        self._responses = responses
        self._status = status
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        body = self._responses.get(request.url)
        if body is None:
            return HttpResponse(status=404, headers=(), body=b"", body_hash=sha256_hex(b""))
        return HttpResponse(status=self._status, headers=(), body=body, body_hash=sha256_hex(body))
