"""Record every completion of a live run, and replay them later without a model server.

`RecordingBackend` wraps a backend (`KITTUM_LLM_RECORD`) and writes each completion under its
request hash (`request_key`, the key `RecordedBackend` uses too), with the request, the model
identity the server reported, the serving, the endpoint, and the exact request and response bytes
from the artifact store. A request asked more than once keeps every answer, in order.
`ReplayBackend` (`KITTUM_LLM_REPLAY`) answers from those recordings in the same order: it puts the
bytes back into its own artifact store, so every hash a verdict or transcript cites resolves, and
fails with `NotReplayed`, naming the request hash and the occurrence, for a request it has no
(further) answer to.

A recording holds no credential and no contact address. A hosted API's key travels only in a
request header, which no backend stores, and the endpoint is recorded without user info or query.
The values that must never be written (`sensitive`: the deployment's keys, passwords, and contact
address) are looked for in the request, the completion, and the raw bytes; where one appears,
recording is refused (`LlmRecordingRefused`, naming the field, not the value) rather than scrubbed,
so every recorded hash still matches its bytes.

A replay is never passed off as a live call. It keeps the model identity and serving that really
answered, and marks the one field every ledger event, verdict attempt, and AI disclosure carries
with them: the endpoint becomes `replay of <recorded endpoint>` (`replayed_endpoint`).

Recordings live in a directory, a `meta.json` and one `<request hash>.json` per request holding
its answers in order, or in one JSON file (a path ending in `.json`) with `meta` and
`completions`, which maps request hashes to the same lists. `meta` names the recipe the live run
used for each role (`judge`), so a replay picks the same one.
"""

import base64
import contextlib
import fcntl
import json
import os
import re
import threading
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote, quote_plus

from kittum.models.types import BackendError, Completion, CompletionRequest, request_key

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from pathlib import Path

    from kittum.models.types import LlmBackend, ModelRecipe

REPLAY_PREFIX = "replay of "
_HASH_FILE = re.compile(r"[0-9a-f]{64}\.json")


class NotReplayed(BackendError):
    """The replay has no (further) recorded answer for this request."""


class LlmRecordingRefused(BackendError):
    """Recording the completion would write a secret or the contact address down."""


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
    """Recorded completions in a directory or a single JSON file. Every write takes a file lock
    and replaces its file atomically, so threads and processes can record into one place."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._single = path.is_file() or (not path.exists() and path.suffix == ".json")
        self._lock = threading.Lock()

    def append(self, key: str, entry: dict[str, Any]) -> None:
        """Add `entry` as the next answer to the request `key`."""

        def add(data: dict[str, Any]) -> None:
            data.setdefault("completions", {}).setdefault(key, []).append(entry)

        if self._single:
            self._update(add)
            return
        with self._locked():
            path = self.path / f"{key}.json"
            entries = json.loads(path.read_text()) if path.exists() else []
            _write_atomic(path, _dumps([*entries, entry]))

    def answers(self, key: str) -> list[dict[str, Any]]:
        """Every recorded answer to the request `key`, in order."""
        if self._single:
            return self._read().get("completions", {}).get(key, [])
        path = self.path / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else []

    def set_role(self, role: str, recipe_id: str) -> None:
        """Note that the live run used `recipe_id` as its `role` (`judge`)."""

        def put(data: dict[str, Any]) -> None:
            data.setdefault("meta", {}).setdefault("roles", {})[role] = recipe_id

        if self._single:
            self._update(put)
            return
        with self._locked():
            path = self.path / "meta.json"
            meta = {"meta": json.loads(path.read_text())} if path.exists() else {}
            put(meta)
            _write_atomic(path, _dumps(meta["meta"]))

    def role(self, role: str) -> str | None:
        """The recipe the live run used as `role`, if it said."""
        if self._single:
            meta = self._read().get("meta", {})
        else:
            path = self.path / "meta.json"
            meta = json.loads(path.read_text()) if path.exists() else {}
        return meta.get("roles", {}).get(role)

    def model_ids(self) -> list[str]:
        """Every recipe ID with a recording."""
        if self._single:
            lists = list(self._read().get("completions", {}).values())
        else:
            files = sorted(self.path.iterdir()) if self.path.is_dir() else []
            lists = [json.loads(p.read_text()) for p in files if _HASH_FILE.fullmatch(p.name)]
        return list(dict.fromkeys(e["request"]["model_id"] for entries in lists for e in entries))

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _update(self, change: Any) -> None:
        with self._locked():
            data = self._read()
            change(data)
            _write_atomic(self.path, _dumps(data))

    @contextlib.contextmanager
    def _locked(self) -> Generator[None]:
        directory = self.path.parent if self._single else self.path
        directory.mkdir(parents=True, exist_ok=True)
        with self._lock, (directory / f".{self.path.name}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _pattern(values: Iterable[str]) -> re.Pattern[bytes] | None:
    """Matches any of `values`, plain or percent-encoded, in any case."""
    spellings = {s for v in values for s in (v, quote(v, safe=""), quote_plus(v))}
    if not spellings:
        return None
    ordered = sorted(spellings, key=len, reverse=True)
    return re.compile(b"|".join(re.escape(s.encode()) for s in ordered), re.IGNORECASE)


class RecordingBackend:
    """Answers as `inner` does and writes each completion to `recordings`, unless it holds one of
    the `sensitive` values, which it refuses to write (and to return)."""

    def __init__(
        self,
        inner: LlmBackend,
        recipe: ModelRecipe,
        recordings: LlmRecordings,
        store: ArtifactStorage,
        *,
        sensitive: Iterable[str | None] = (),
    ) -> None:
        self._inner = inner
        self.recipe = recipe
        self._recordings = recordings
        self._store = store
        self._sensitive = _pattern(s for s in sensitive if s)

    def complete(self, request: CompletionRequest) -> Completion:
        completion = self._inner.complete(request)
        raw_request = self._stored(completion.raw_request_hash)
        raw_response = self._stored(completion.raw_response_hash)
        if self._sensitive is not None:
            for field, data in (
                ("request", json.dumps(request.model_dump(mode="json"), ensure_ascii=False)),
                ("completion", json.dumps(completion.model_dump(mode="json"), ensure_ascii=False)),
                ("raw request", raw_request),
                ("raw response", raw_response),
            ):
                found = data.encode() if isinstance(data, str) else data
                if found is not None and self._sensitive.search(found):
                    raise LlmRecordingRefused(
                        f"{self.recipe.id}: not recording request {completion.request_hash}: its"
                        f" {field} holds a configured secret or the contact address"
                    )
        self._recordings.append(
            completion.request_hash,
            {
                "request": request.model_dump(mode="json"),
                "completion": completion.model_dump(mode="json"),
                "raw_request": _encode(raw_request),
                "raw_response": _encode(raw_response),
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
    """Serves `recipe` from `recordings` only, each request's answers in their recorded order.
    Nothing is sent anywhere."""

    def __init__(self, recipe: ModelRecipe, recordings: LlmRecordings, store: ArtifactStorage):
        self.recipe = recipe
        self._recordings = recordings
        self._store = store
        self._asked: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def complete(self, request: CompletionRequest) -> Completion:
        if request.model_id != self.recipe.id:
            raise ValueError(f"backend serves {self.recipe.id}, not {request.model_id}")
        key = request_key(request)
        answers = self._recordings.answers(key)
        with self._lock:
            number = self._asked[key]
            self._asked[key] += 1
        if number >= len(answers):
            raise NotReplayed(
                f"{self.recipe.id}: no recorded completion {number + 1} for request {key}"
                f" in {self._recordings.path} (it has {len(answers)})"
            )
        entry = answers[number]
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


__all__ = [
    "REPLAY_PREFIX",
    "ArtifactStorage",
    "LlmRecordingRefused",
    "LlmRecordings",
    "NotReplayed",
    "RecordingBackend",
    "ReplayBackend",
    "replayed_endpoint",
]
