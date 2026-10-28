"""Record every completion of a live run, and replay them later without a model server.

`RecordingBackend` wraps a backend (`KITTUM_LLM_RECORD`) and writes each completion under its
request hash (`request_key`, the key `RecordedBackend` uses too), with the request, the model
identity the server reported, the serving, the endpoint, and the exact request and response bytes
from the artifact store. `ReplayBackend` (`KITTUM_LLM_REPLAY`) answers from those recordings: it
puts the bytes back into its own artifact store, so every hash a verdict or transcript cites
resolves, and fails with `NotReplayed`, naming the request hash, for a request never recorded.

A recording holds no credential: a hosted API's key travels only in a request header, which no
backend stores, and the endpoint is recorded without user info or query.

A replay is never passed off as a live call. It keeps the model identity and serving that really
answered, and marks the one field every ledger event, verdict attempt, and AI disclosure carries
with them: the endpoint becomes `replay of <recorded endpoint>` (`replayed_endpoint`).

Recordings live in a directory, one `<request hash>.json` per completion, or in one JSON file
(a path ending in `.json`) that maps request hashes to the same entries.
"""

import base64
import contextlib
import fcntl
import json
import os
import threading
from typing import TYPE_CHECKING, Any, Protocol

from kittum.models.types import BackendError, Completion, CompletionRequest, request_key

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from kittum.models.types import LlmBackend, ModelRecipe

REPLAY_PREFIX = "replay of "


class NotReplayed(BackendError):
    """The replay has no recording for this request."""


class ArtifactStorage(Protocol):
    """The part of `kittum.storage.ArtifactStore` recording and replaying need."""

    def put(self, data: bytes) -> str: ...

    def get(self, sha256: str) -> bytes: ...


def replayed_endpoint(endpoint: str | None) -> str:
    """The endpoint a replayed completion reports."""
    return REPLAY_PREFIX + endpoint if endpoint else REPLAY_PREFIX.strip()


def _encode(data: bytes | None) -> dict[str, str] | None:
    if data is None:
        return None
    try:
        return {"text": data.decode()}
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(data).decode("ascii")}


def _decode(value: dict[str, str] | None) -> bytes | None:
    if value is None:
        return None
    if "text" in value:
        return value["text"].encode()
    return base64.b64decode(value["base64"])


class LlmRecordings:
    """Recorded completions in a directory or a single JSON file, safe across threads and
    processes: a directory gets one file per request, a file is merged under a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._single = path.is_file() or (not path.exists() and path.suffix == ".json")
        self._lock = threading.Lock()

    def write(self, key: str, entry: dict[str, Any]) -> None:
        with self._lock:
            if not self._single:
                self.path.mkdir(parents=True, exist_ok=True)
                _write_atomic(self.path / f"{key}.json", _dumps(entry))
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _flock(self.path.with_name(f".{self.path.name}.lock")):
                entries = json.loads(self.path.read_text()) if self.path.exists() else {}
                entries[key] = entry
                _write_atomic(self.path, _dumps(entries))

    def read(self, key: str) -> dict[str, Any] | None:
        if self._single:
            if not self.path.exists():
                return None
            return json.loads(self.path.read_text()).get(key)
        path = self.path / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def model_ids(self) -> list[str]:
        """Every recipe ID with a recording, in order of first appearance by key."""
        if self._single:
            entries = list(json.loads(self.path.read_text()).values()) if self.path.exists() else []
        else:
            files = sorted(self.path.glob("*.json")) if self.path.is_dir() else []
            entries = [json.loads(p.read_text()) for p in files]
        return list(dict.fromkeys(e["request"]["model_id"] for e in entries))


class RecordingBackend:
    """Answers as `inner` does and writes each completion to `recordings`."""

    def __init__(
        self,
        inner: LlmBackend,
        recipe: ModelRecipe,
        recordings: LlmRecordings,
        store: ArtifactStorage,
    ) -> None:
        self._inner = inner
        self.recipe = recipe
        self._recordings = recordings
        self._store = store

    def complete(self, request: CompletionRequest) -> Completion:
        completion = self._inner.complete(request)
        self._recordings.write(
            completion.request_hash,
            {
                "request": request.model_dump(mode="json"),
                "completion": completion.model_dump(mode="json"),
                "raw_request": _encode(self._stored(completion.raw_request_hash)),
                "raw_response": _encode(self._stored(completion.raw_response_hash)),
            },
        )
        return completion

    def _stored(self, sha256: str | None) -> bytes | None:
        """The bytes behind a hash, if the backend stored them (a fake may only hash its text)."""
        if sha256 is None:
            return None
        try:
            return self._store.get(sha256)
        except KeyError, FileNotFoundError, ValueError:
            return None


class ReplayBackend:
    """Serves `recipe` from `recordings` only. Nothing is sent anywhere."""

    def __init__(self, recipe: ModelRecipe, recordings: LlmRecordings, store: ArtifactStorage):
        self.recipe = recipe
        self._recordings = recordings
        self._store = store

    def complete(self, request: CompletionRequest) -> Completion:
        if request.model_id != self.recipe.id:
            raise ValueError(f"backend serves {self.recipe.id}, not {request.model_id}")
        key = request_key(request)
        entry = self._recordings.read(key)
        if entry is None:
            raise NotReplayed(
                f"{self.recipe.id}: no recorded completion for request {key}"
                f" in {self._recordings.path}"
            )
        recorded = Completion.model_validate(entry["completion"])
        if recorded.request_hash != key:
            raise BackendError(
                f"{self.recipe.id}: the recording under {key} is for another request"
            )
        for field in ("raw_request", "raw_response"):
            data = _decode(entry.get(field))
            expected = getattr(recorded, f"{field}_hash")
            if data is not None and self._store.put(data) != expected:
                raise BackendError(f"{self.recipe.id}: the recorded {field} of {key} was altered")
        return recorded.model_copy(update={"endpoint": replayed_endpoint(recorded.endpoint)})


def _dumps(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    temporary.write_bytes(data)
    temporary.replace(path)


@contextlib.contextmanager
def _flock(path: Path) -> Generator[None]:
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


__all__ = [
    "REPLAY_PREFIX",
    "ArtifactStorage",
    "LlmRecordings",
    "NotReplayed",
    "RecordingBackend",
    "ReplayBackend",
    "replayed_endpoint",
]
