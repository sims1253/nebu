"""Persistence for review metadata.

`ReviewStore` is the interface. Two backends implement it: in-memory (the
default when JANUS_REVIEW_STORE is unset) and SQLite (JANUS_REVIEW_STORE=sqlite).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Protocol

from janus.config import data_root
from janus.schemas.review import ReviewMetadata, ReviewStatus

# Allowed status transitions. READY is terminal. Only ERROR can be left, back
# into VALIDATING_INPUTS when a failed review is retried.
_VALID_TRANSITIONS: dict[ReviewStatus, set[ReviewStatus]] = {
    ReviewStatus.CREATED: {ReviewStatus.VALIDATING_INPUTS, ReviewStatus.ERROR},
    ReviewStatus.VALIDATING_INPUTS: {ReviewStatus.READING_DOCUMENT, ReviewStatus.ERROR},
    ReviewStatus.READING_DOCUMENT: {ReviewStatus.EXTRACTING_FIELDS, ReviewStatus.ERROR},
    ReviewStatus.EXTRACTING_FIELDS: {ReviewStatus.COMPARING, ReviewStatus.ERROR},
    ReviewStatus.COMPARING: {ReviewStatus.READY, ReviewStatus.ERROR},
    ReviewStatus.READY: set(),
    ReviewStatus.ERROR: {ReviewStatus.VALIDATING_INPUTS},
}


class ReviewStore(Protocol):
    async def create(self, metadata: ReviewMetadata) -> ReviewMetadata: ...
    async def get(self, review_id: str) -> ReviewMetadata | None: ...
    async def list_all(self) -> list[ReviewMetadata]: ...
    async def update_status(
        self, review_id: str, status: ReviewStatus, error_message: str | None = None
    ) -> ReviewMetadata | None: ...
    async def delete(self, review_id: str) -> bool: ...


class InMemoryReviewStore:
    """Default backend when JANUS_REVIEW_STORE is unset. Keeps everything in
    process memory; a restart loses all reviews. Suited to tests and throwaway
    runs."""

    def __init__(self) -> None:
        self._reviews: dict[str, ReviewMetadata] = {}
        self._lock = asyncio.Lock()

    async def create(self, metadata: ReviewMetadata) -> ReviewMetadata:
        async with self._lock:
            if metadata.id in self._reviews:
                raise ValueError(f"review already exists: {metadata.id}")
            self._reviews[metadata.id] = metadata.model_copy(deep=True)
            return metadata

    async def get(self, review_id: str) -> ReviewMetadata | None:
        item = self._reviews.get(review_id)
        return item.model_copy(deep=True) if item else None

    async def list_all(self) -> list[ReviewMetadata]:
        return sorted(
            (item.model_copy(deep=True) for item in self._reviews.values()),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def update_status(
        self, review_id: str, status: ReviewStatus, error_message: str | None = None
    ) -> ReviewMetadata | None:
        async with self._lock:
            item = self._reviews.get(review_id)
            if item is None:
                return None
            if status not in _VALID_TRANSITIONS[item.status]:
                raise ValueError(f"invalid review status transition: {item.status} -> {status}")
            updated = item.model_copy(update={"status": status, "error_message": error_message})
            self._reviews[review_id] = updated
            return updated.model_copy(deep=True)

    async def delete(self, review_id: str) -> bool:
        async with self._lock:
            return self._reviews.pop(review_id, None) is not None


class SqliteReviewStore:
    """Stores each review as one JSON row in its own reviews_v2 table.

    The table name is versioned so rows from older schemas are never read as
    v2 data. sqlite3 is synchronous, so every call runs in a worker thread and
    every connection is closed. update_status checks the transition inside a
    write transaction, so concurrent workers cannot skip the state machine.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reviews_v2 "
                "(id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.isolation_level = None  # explicit transaction control
        return connection

    async def create(self, metadata: ReviewMetadata) -> ReviewMetadata:
        def _create() -> None:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "INSERT INTO reviews_v2(id, metadata_json) VALUES (?, ?)",
                        (metadata.id, metadata.model_dump_json()),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise

        await asyncio.to_thread(_create)
        return metadata

    async def get(self, review_id: str) -> ReviewMetadata | None:
        def _get() -> ReviewMetadata | None:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT metadata_json FROM reviews_v2 WHERE id = ?", (review_id,)
                ).fetchone()
            return ReviewMetadata.model_validate_json(row[0]) if row else None

        return await asyncio.to_thread(_get)

    async def list_all(self) -> list[ReviewMetadata]:
        def _list_all() -> list[ReviewMetadata]:
            with closing(self._connect()) as connection:
                rows = connection.execute("SELECT metadata_json FROM reviews_v2").fetchall()
            return [ReviewMetadata.model_validate_json(row[0]) for row in rows]

        items = await asyncio.to_thread(_list_all)
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def update_status(
        self, review_id: str, status: ReviewStatus, error_message: str | None = None
    ) -> ReviewMetadata | None:
        def _update_status() -> ReviewMetadata | None:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT metadata_json FROM reviews_v2 WHERE id = ?", (review_id,)
                    ).fetchone()
                    if row is None:
                        connection.execute("COMMIT")
                        return None
                    item = ReviewMetadata.model_validate_json(row[0])
                    if status not in _VALID_TRANSITIONS[item.status]:
                        raise ValueError(
                            f"invalid review status transition: {item.status} -> {status}"
                        )
                    updated = item.model_copy(
                        update={"status": status, "error_message": error_message}
                    )
                    connection.execute(
                        "UPDATE reviews_v2 SET metadata_json = ? WHERE id = ?",
                        (updated.model_dump_json(), review_id),
                    )
                    connection.execute("COMMIT")
                    return updated
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise

        return await asyncio.to_thread(_update_status)

    async def delete(self, review_id: str) -> bool:
        def _delete() -> bool:
            with closing(self._connect()) as connection:
                cursor = connection.execute("DELETE FROM reviews_v2 WHERE id = ?", (review_id,))
                connection.commit()
                return cursor.rowcount > 0

        return await asyncio.to_thread(_delete)


_store: ReviewStore | None = None


def get_review_store() -> ReviewStore:
    global _store
    if _store is None:
        kind = os.getenv("JANUS_REVIEW_STORE", "memory")
        if kind == "sqlite":
            path = Path(os.getenv("JANUS_REVIEW_DB_PATH", str(data_root() / "reviews-v2.sqlite3")))
            _store = SqliteReviewStore(path)
        else:
            _store = InMemoryReviewStore()
    return _store


# Test hooks: reset drops back to the environment-selected backend; set
# installs a specific store (both clear the process-wide singleton).
def reset_review_store() -> None:
    global _store
    _store = None


def set_review_store(store: ReviewStore | None) -> None:
    global _store
    _store = store
