"""Record the real sources' responses once, and answer from them later.

A recordings directory holds a `manifest.json` that maps each request URL to its recorded body,
either as a file name or as `{"file": ..., "status": ..., "content_type": ...}`. A file name may
point outside the directory (a PDF the repository holds elsewhere). A request for a URL that was
never recorded fails as an unreachable source would, so a stack that serves the gateway from
recordings (`KITTUM_GATEWAY_RECORDINGS`) never touches the network.

`RecordingTransport` writes that directory from a live run (`KITTUM_GATEWAY_RECORD`). It keys each
response by `recording_key`: the URL with its query, without credentials, and with the contact
address replaced by `CONTACT_PLACEHOLDER`, which a body that echoes the address gets too. Request
headers, where API keys travel, are never written. Of the response headers only `Content-Type` is
kept: nothing downstream of the gateway reads another, and replaying a source's rate-limit headers
offline would only slow the replay down.
"""

import contextlib
import fcntl
import json
import os
import threading
from typing import TYPE_CHECKING
from urllib.parse import quote, quote_plus, urlencode

from kittum.core import sha256_hex
from kittum.gateway.types import HttpRequest, HttpResponse, effective_request

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from pathlib import Path

    from kittum.gateway.types import Transport

CONTACT_PLACEHOLDER = "contact@example.invalid"
"""Stands for the deployment's contact address in every recording."""

CREDENTIAL_PARAMS = frozenset({"api_key", "apikey", "key", "token", "access_token"})
"""Query parameters that carry a credential (NCBI's `api_key`), dropped from recording keys."""

_EXTENSIONS = {"application/json": ".json", "application/pdf": ".pdf", "text/html": ".html"}


class NotRecorded(ConnectionError):
    """The request's URL has no recording: offline, the source is unreachable."""


def _spellings(value: str) -> tuple[str, ...]:
    """`value` as it may appear in a URL or a body: plain, and percent-encoded both ways."""
    return tuple(dict.fromkeys((quote_plus(value), quote(value, safe=""), value)))


def _replace_contact(text: str, contact_email: str | None) -> str:
    if not contact_email:
        return text
    for spelling, placeholder in zip(
        _spellings(contact_email), _spellings(CONTACT_PLACEHOLDER), strict=True
    ):
        text = text.replace(spelling, placeholder)
    return text


def recording_key(request: HttpRequest, contact_email: str | None = None) -> str:
    """The manifest key of `request`: its URL and query, safe to write down.

    Credential parameters are dropped and the contact address becomes `CONTACT_PLACEHOLDER`, so
    the key holds no secret and no personal data, and a replay with any contact address finds it.
    """
    request = effective_request(request)
    params = [(k, v) for k, v in request.params if k.lower() not in CREDENTIAL_PARAMS]
    url = request.url + ("?" + urlencode(params) if params else "")
    return _replace_contact(url, contact_email)


class RecordedTransport:
    def __init__(self, directory: Path, *, contact_email: str | None = None) -> None:
        manifest = json.loads((directory / "manifest.json").read_text())
        self._contact_email = contact_email
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
        # A hand-made manifest keys the URL as sent; a recorded one keys `recording_key`.
        found = (None if request.params else self._responses.get(request.url)) or (
            self._responses.get(recording_key(request, self._contact_email))
        )
        if found is None:
            raise NotRecorded(f"no recorded response for {request.url}")
        path, status, content_type = found
        body = path.read_bytes()
        headers = (("Content-Type", content_type),) if content_type else ()
        return HttpResponse(status=status, headers=headers, body=body, body_hash=sha256_hex(body))


class RecordingTransport:
    """Sends through `inner` and writes every response it gets into `directory`, in the format
    `RecordedTransport` reads. Each attempt is written, so a retried request keeps its last answer.

    The manifest is merged under a file lock and replaced atomically after every response, so the
    server and its workers can record into one directory and a crash keeps what came before it.
    `secrets` (API keys the deployment sends) are redacted from bodies that echo them.
    """

    def __init__(
        self,
        inner: Transport,
        directory: Path,
        *,
        contact_email: str | None = None,
        secrets: Iterable[str | None] = (),
    ) -> None:
        self._inner = inner
        self._directory = directory
        self._contact_email = contact_email
        self._secrets = tuple(s for s in secrets if s)
        self._lock = threading.Lock()
        directory.mkdir(parents=True, exist_ok=True)

    def send(self, request: HttpRequest) -> HttpResponse:
        response = self._inner.send(request)
        self._write(request, response)
        return response

    def _redact(self, body: bytes) -> bytes:
        for secret in self._secrets:
            for spelling in _spellings(secret):
                body = body.replace(spelling.encode(), b"[redacted]")
        if self._contact_email:
            for spelling, placeholder in zip(
                _spellings(self._contact_email), _spellings(CONTACT_PLACEHOLDER), strict=True
            ):
                body = body.replace(spelling.encode(), placeholder.encode())
        return body

    def _write(self, request: HttpRequest, response: HttpResponse) -> None:
        content_type = next(
            (v.split(";")[0].strip() for k, v in response.headers if k.lower() == "content-type"),
            None,
        )
        body = self._redact(response.body)
        name = sha256_hex(body) + _EXTENSIONS.get(content_type or "", "")
        entry = {"file": name, "status": response.status, "content_type": content_type}
        key = recording_key(request, self._contact_email)
        with self._lock, self._locked():
            path = self._directory / name
            if not path.exists():
                _write_atomic(path, body)
            manifest_path = self._directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            manifest[key] = entry
            _write_atomic(
                manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
            )

    @contextlib.contextmanager
    def _locked(self) -> Generator[None]:
        with (self._directory / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    temporary.write_bytes(data)
    temporary.replace(path)


__all__ = [
    "CONTACT_PLACEHOLDER",
    "NotRecorded",
    "RecordedTransport",
    "RecordingTransport",
    "recording_key",
]
