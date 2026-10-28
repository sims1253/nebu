"""Test doubles for the search module."""

from typing import TYPE_CHECKING

from kittum.core import SystemClock, new_id, sha256_hex
from kittum.search.models import SearchExecution, SearchPlan, Source, SourceRecord

if TYPE_CHECKING:
    from collections.abc import Mapping


class FakeSource(Source):
    """A fake search source returning canned executions or records."""

    def __init__(
        self,
        name: str = "fake",
        *,
        records: tuple[SourceRecord, ...] = (),
        executions: Mapping[str, SearchExecution] | None = None,
        reported_count: int | None = None,
        query_translation: str | None = None,
    ) -> None:
        self.name = name
        self._records = records
        self._executions = executions or {}
        self._reported_count = reported_count
        self._query_translation = query_translation
        self.plans: list[SearchPlan] = []

    def run(self, plan: SearchPlan) -> SearchExecution:
        self.plans.append(plan)
        if plan.strategy in self._executions:
            return self._executions[plan.strategy]

        now = SystemClock().now()
        recs = self._records
        rep_count = self._reported_count if self._reported_count is not None else len(recs)
        raw_hash = sha256_hex(b"fake-search-body")

        # Ensure records have raw_hash and occurrence_ids
        prepared_recs = []
        for i, r in enumerate(recs):
            rec = r.model_copy(
                update={
                    "occurrence_id": r.occurrence_id or new_id(),
                    "ordinal": i,
                    "raw_hash": r.raw_hash or raw_hash,
                }
            )
            prepared_recs.append(rec)

        return SearchExecution(
            id=new_id(),
            plan=plan,
            kind="live_api",
            started_at=now,
            ended_at=now,
            query_translation=self._query_translation,
            reported_count=rep_count,
            retrieved_count=len(prepared_recs),
            raw_artifact_hashes=(raw_hash,),
            records=tuple(prepared_recs),
        )


__all__ = ["FakeSource"]
