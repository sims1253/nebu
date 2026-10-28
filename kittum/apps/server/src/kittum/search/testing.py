"""Test doubles for the search module."""

from typing import TYPE_CHECKING

from kittum.core import SystemClock, new_id
from kittum.search.models import SearchExecution, SearchPlan, Source, SourceRecord

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kittum.storage import ArtifactStore


class FakeSource(Source):
    """A fake search source returning canned executions or records.

    Like a real source, it stores its one raw response body in `artifacts` and points every record
    at it. Canned `executions` are returned as given.
    """

    def __init__(
        self,
        artifacts: ArtifactStore,
        name: str = "fake",
        *,
        records: tuple[SourceRecord, ...] = (),
        executions: Mapping[str, SearchExecution] | None = None,
        reported_count: int | None = None,
        query_translation: str | None = None,
        body: bytes = b"fake-search-body",
    ) -> None:
        self.name = name
        self._artifacts = artifacts
        self._records = records
        self._executions = executions or {}
        self._reported_count = reported_count
        self._query_translation = query_translation
        self._body = body
        self.plans: list[SearchPlan] = []

    def run(self, plan: SearchPlan) -> SearchExecution:
        self.plans.append(plan)
        if plan.strategy in self._executions:
            return self._executions[plan.strategy]

        now = SystemClock().now()
        raw_hash = self._artifacts.put(self._body)
        records = tuple(
            r.model_copy(update={"ordinal": i, "raw_hash": raw_hash})
            for i, r in enumerate(self._records)
        )
        return SearchExecution(
            id=new_id(),
            plan=plan,
            kind="live_api",
            started_at=now,
            ended_at=now,
            query_translation=self._query_translation,
            reported_count=(
                self._reported_count if self._reported_count is not None else len(records)
            ),
            retrieved_count=len(records),
            raw_artifact_hashes=(raw_hash,),
            records=records,
        )


__all__ = ["FakeSource"]
