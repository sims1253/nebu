"""A transport that answers from responses recorded once from the real sources.

A recordings directory holds a `manifest.json` that maps each request URL to its recorded body,
either as a file name or as `{"file": ..., "status": ..., "content_type": ...}`. A file name may
point outside the directory (a PDF the repository holds elsewhere). A request for a URL that was
never recorded fails as an unreachable source would, so a stack that serves the gateway from
recordings (`KITTUM_GATEWAY_RECORDINGS`) never touches the network.
"""

import json
from typing import TYPE_CHECKING

from kittum.core import sha256_hex
from kittum.gateway.types import HttpRequest, HttpResponse

if TYPE_CHECKING:
    from pathlib import Path


class NotRecorded(ConnectionError):
    """The request's URL has no recording: offline, the source is unreachable."""


class RecordedTransport:
    def __init__(self, directory: Path) -> None:
        manifest = json.loads((directory / "manifest.json").read_text())
        self._responses: dict[str, tuple[Path, int, str | None]] = {}
        for url, entry in manifest.items():
            if isinstance(entry, str):
                entry = {"file": entry}
            self._responses[url] = (
                (directory / entry["file"]).resolve(),
                int(entry.get("status", 200)),
                entry.get("content_type"),
            )
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        found = self._responses.get(request.url)
        if found is None:
            raise NotRecorded(f"no recorded response for {request.url}")
        path, status, content_type = found
        body = path.read_bytes()
        headers = (("Content-Type", content_type),) if content_type else ()
        return HttpResponse(status=status, headers=headers, body=body, body_hash=sha256_hex(body))


__all__ = ["NotRecorded", "RecordedTransport"]
