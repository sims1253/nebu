"""Content-addressed artifact storage (DESIGN.md, "Store").

PDFs, raw API responses, model outputs, and rendered reports are stored once under their SHA-256.
Rows and ledger events reference the hash. Upload before committing the reference, so a crash
leaves an orphan artifact, never a dangling reference.
"""

import os
import tempfile
from pathlib import Path
from typing import Protocol

from kittum.core import Sha256, sha256_hex


class ArtifactStore(Protocol):
    def put(self, data: bytes) -> Sha256: ...

    def get(self, sha256: str) -> bytes: ...

    def exists(self, sha256: str) -> bool: ...


class FilesystemStore:
    """Stores artifacts as files under `root/ab/cd/<hash>`. Writes are atomic and idempotent."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, sha256: str) -> Path:
        return self._root / sha256[:2] / sha256[2:4] / sha256

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{digest}.")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            Path(tmp).replace(path)
        return digest

    def get(self, sha256: str) -> bytes:
        data = self._path(sha256).read_bytes()
        if sha256_hex(data) != sha256:
            raise ValueError(f"artifact {sha256} is corrupt")
        return data

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        self._data[digest] = data
        return digest

    def get(self, sha256: str) -> bytes:
        return self._data[sha256]

    def exists(self, sha256: str) -> bool:
        return sha256 in self._data


__all__ = ["ArtifactStore", "FilesystemStore", "MemoryStore"]
