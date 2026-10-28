"""Record the real sources' responses once, and answer from them later.

A recordings directory holds a `manifest.json` that maps each request URL to its recorded
responses. An entry is a file name, `{"file": ..., "status": ..., "content_type": ...}`, or a list
of those; `{"error": <kind>}` stands for a failure to get a response, such as httpx's
`ConnectError`, `DecodingError`, or `TooManyRedirects`, and raises the same kind again. A single
entry, as in a hand-made manifest, answers every time; a list holds the responses and failures in
the order they came and is answered in that order, one per request, so a source that failed and
later recovered fails and recovers again. A file name may point outside the directory
(a PDF the repository holds elsewhere). A request with nothing (left) to answer it fails as an
unreachable source would, so a stack that serves the gateway from recordings
(`KITTUM_GATEWAY_RECORDINGS`) never touches the network.

`RecordingTransport` writes that directory from a live run (`KITTUM_GATEWAY_RECORD`), each attempt
in order. It keys each response by `recording_key`: the URL and query without credential
parameters, and with the contact address replaced by `CONTACT_PLACEHOLDER` in the parameter each
source takes it in (`CONTACT_PARAMS`). It refuses, before anything is sent, a URL with user info
or a configured secret or the contact address anywhere else, naming where but not the value. A
response body that holds a secret or the address is cleaned (`[redacted]`, the placeholder), and
the live run gets the cleaned response too, so the live run and its replay see the same bytes and
hashes. Request headers, where API keys travel, are never written. Of the response headers only
`Content-Type` is kept: nothing downstream of the gateway reads another, and replaying a source's
rate-limit headers offline would only slow the replay down.
"""

import builtins
import contextlib
import fcntl
import json
import os
import re
import threading
from collections import defaultdict
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus, urlencode, urlsplit

import httpx2

from kittum.core import sha256_hex
from kittum.gateway.errors import GatewayError
from kittum.gateway.types import HttpRequest, HttpResponse, effective_request

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from pathlib import Path

    from kittum.gateway.types import Transport

CONTACT_PLACEHOLDER = "contact@example.invalid"
"""Stands for the deployment's contact address in every recording."""

CONTACT_PARAMS = {
    "api.unpaywall.org": "email",
    "api.crossref.org": "mailto",
    "eutils.ncbi.nlm.nih.gov": "email",
    "api.openalex.org": "mailto",
}
"""The query parameter each source takes the contact address in, by host."""

CREDENTIAL_PARAMS = frozenset({"api_key", "apikey", "key", "token", "access_token"})
"""Query parameters that carry a credential (NCBI's `api_key`), dropped from recording keys."""

_EXTENSIONS = {"application/json": ".json", "application/pdf": ".pdf", "text/html": ".html"}


class NotRecorded(ConnectionError):
    """The request has no recorded response (left): offline, the source is unreachable."""


class RecordingRefused(GatewayError):
    """Recording the request would write a secret or the contact address down."""


def _spellings(value: str) -> tuple[str, ...]:
    """`value` as it may appear in a URL or a body: plain, and percent-encoded both ways."""
    return tuple(dict.fromkeys((quote_plus(value), quote(value, safe=""), value)))


def _pattern(values: Iterable[str]) -> re.Pattern[bytes] | None:
    spellings = sorted({s for v in values for s in _spellings(v)}, key=len, reverse=True)
    if not spellings:
        return None
    return re.compile(b"|".join(re.escape(s.encode()) for s in spellings), re.IGNORECASE)


def _holds(text: str, value: str) -> bool:
    folded = text.lower()
    return any(s.lower() in folded for s in _spellings(value))


def recording_key(
    request: HttpRequest, contact_email: str | None = None, secrets: Iterable[str] = ()
) -> str:
    """The manifest key of `request`: its URL and query, safe to write down.

    Credential parameters are dropped and the contact address in its source's contact parameter
    becomes `CONTACT_PLACEHOLDER`, so a replay with any contact address finds the key. A URL with
    user info, or with the address or one of `secrets` anywhere else, raises `RecordingRefused`.
    """
    request = effective_request(request)
    split = urlsplit(request.url)
    if "@" in split.netloc:
        raise RecordingRefused(f"{split.hostname}: the URL carries user info")
    contact_param = CONTACT_PARAMS.get(split.hostname or "")
    params = []
    for name, value in request.params:
        if name.lower() in CREDENTIAL_PARAMS:
            continue
        if contact_email and name == contact_param and value.lower() == contact_email.lower():
            value = CONTACT_PLACEHOLDER
        params.append((name, value))
    sensitive = [s for s in (contact_email, *secrets) if s]
    for value in sensitive:
        where = next((f"parameter {n!r}" for n, v in params if _holds(v, value)), None)
        if where is None and _holds(request.url, value):
            where = "the path"
        if where is not None:
            raise RecordingRefused(
                f"{split.hostname}: {where} carries a configured secret or the contact address"
            )
    return request.url + ("?" + urlencode(params) if params else "")


class RecordedTransport:
    def __init__(self, directory: Path, *, contact_email: str | None = None) -> None:
        manifest = json.loads((directory / "manifest.json").read_text())
        self._contact_email = contact_email
        # A list answers in order, once each; a single entry answers every time.
        self._responses: dict[str, tuple[list[_Entry], bool]] = {}
        for url, entry in manifest.items():
            entries = entry if isinstance(entry, list) else [entry]
            self._responses[url] = (
                [_response_entry(directory, e) for e in entries],
                isinstance(entry, list),
            )
        self._used: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        key = request.url if not request.params and request.url in self._responses else None
        if key is None:
            try:
                key = recording_key(request, self._contact_email)
            except RecordingRefused:
                key = None
        if key is None or key not in self._responses:
            raise NotRecorded(f"no recorded response for {request.url}")
        entries, in_order = self._responses[key]
        with self._lock:
            number = self._used[key]
            self._used[key] += 1
        if in_order and number >= len(entries):
            raise NotRecorded(
                f"no recorded response {number + 1} for {request.url}"
                f" (the recording has {len(entries)})"
            )
        entry = entries[number if in_order else 0]
        if isinstance(entry, BaseException):
            raise entry
        path, status, content_type = entry
        body = path.read_bytes()
        headers = (("Content-Type", content_type),) if content_type else ()
        return HttpResponse(status=status, headers=headers, body=body, body_hash=sha256_hex(body))


type _Entry = tuple[Path, int, str | None] | Exception


def _failure(kind: str) -> Exception:
    """The error a recorded failure stands for: the httpx error of that name (`DecodingError`,
    `TooManyRedirects`, the `TransportError` family, any `HTTPError` built from a message), or the
    builtin connection error. Anything else, a kind this version does not know included, becomes
    a `ConnectionError`, as an unreachable source would."""
    message = f"recorded failure: {kind}"
    found = getattr(httpx2, kind, None)
    if isinstance(found, type) and issubclass(found, httpx2.HTTPError):
        try:
            return found(message)
        except TypeError:  # one that needs a request and response, which no transport raises
            return ConnectionError(message)
    found = getattr(builtins, kind, None)
    if isinstance(found, type) and issubclass(found, OSError):
        return found(message)
    return ConnectionError(message)


def _response_entry(directory: Path, entry: str | dict[str, Any]) -> _Entry:
    if isinstance(entry, str):
        entry = {"file": entry}
    if "error" in entry:
        return _failure(entry["error"])
    return (
        (directory / entry["file"]).resolve(),
        int(entry.get("status", 200)),
        entry.get("content_type"),
    )


class RecordingTransport:
    """Sends through `inner` and writes every response it gets into `directory`, in the format
    `RecordedTransport` reads, in the order they came, each attempt of a retried request included.

    `secrets` are the values no recording may hold: the deployment's API keys, tokens, and
    passwords. The manifest is merged under a file lock and replaced atomically after every
    response, so the server and its workers can record into one directory and a crash keeps what
    came before it.
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
        self._redact = _pattern(self._secrets)
        self._contact = _pattern([contact_email] if contact_email else [])
        self._lock = threading.Lock()
        directory.mkdir(parents=True, exist_ok=True)

    def send(self, request: HttpRequest) -> HttpResponse:
        key = recording_key(request, self._contact_email, self._secrets)
        try:
            response = self._inner.send(request)
        except Exception as error:
            # Only the kind: a message may quote the URL, and with it the contact address.
            self._append(key, {"error": type(error).__name__})
            raise
        body = self._clean(response.body)
        if body != response.body:
            response = response.model_copy(update={"body": body, "body_hash": sha256_hex(body)})
        self._write(key, response)
        return response

    def _clean(self, body: bytes) -> bytes:
        if self._redact is not None:
            body = self._redact.sub(b"[redacted]", body)
        if self._contact is not None:
            placeholder = quote_plus(CONTACT_PLACEHOLDER).encode()
            plain = CONTACT_PLACEHOLDER.encode()
            body = self._contact.sub(lambda m: placeholder if b"%" in m[0] else plain, body)
        return body

    def _write(self, key: str, response: HttpResponse) -> None:
        content_type = next(
            (v.split(";")[0].strip() for k, v in response.headers if k.lower() == "content-type"),
            None,
        )
        name = response.body_hash + _EXTENSIONS.get(content_type or "", "")
        path = self._directory / name
        with self._lock, self._locked():
            if not path.exists():
                _write_atomic(path, response.body)
        self._append(key, {"file": name, "status": response.status, "content_type": content_type})

    def _append(self, key: str, entry: dict[str, Any]) -> None:
        with self._lock, self._locked():
            manifest_path = self._directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            manifest.setdefault(key, []).append(entry)
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
    "CONTACT_PARAMS",
    "CONTACT_PLACEHOLDER",
    "NotRecorded",
    "RecordedTransport",
    "RecordingRefused",
    "RecordingTransport",
    "recording_key",
]
