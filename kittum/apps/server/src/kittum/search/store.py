"""Postgres persistence for search executions and source occurrences."""

import json
from typing import TYPE_CHECKING, Any

from kittum.search.models import SearchExecution, SearchPlan, SourceRecord

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Tx


def _is_null_tx(tx: Any) -> bool:
    return type(tx).__name__ == "NullTx"


def save_execution(tx: Tx, project_id: Id, execution: SearchExecution) -> None:
    """Save a search execution and all its source occurrences."""
    if _is_null_tx(tx):
        return

    tx.execute(
        """
        insert into search_execution (
            id, project_id, topic_id, source, platform, kind,
            strategy, limits, query_translation, reported_count,
            retrieved_count, started_at, ended_at, raw_artifact_hashes
        ) values (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        """,
        (
            execution.id,
            project_id,
            execution.plan.topic_id,
            execution.plan.source,
            execution.plan.platform,
            execution.kind,
            execution.plan.strategy,
            json.dumps(list(execution.plan.limits)),
            execution.query_translation,
            execution.reported_count,
            execution.retrieved_count,
            execution.started_at,
            execution.ended_at,
            json.dumps(list(execution.raw_artifact_hashes)),
        ),
    )

    for record in execution.records:
        tx.execute(
            """
            insert into source_occurrence (
                occurrence_id, execution_id, project_id, source,
                provider_record_id, ordinal, title, abstract,
                authors, year, journal, doi, pmid,
                registry_ids, publication_types, raw_hash
            ) values (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            """,
            (
                record.occurrence_id,
                execution.id,
                project_id,
                record.source,
                record.provider_record_id,
                record.ordinal,
                record.title,
                record.abstract,
                json.dumps(list(record.authors)),
                record.year,
                record.journal,
                record.doi,
                record.pmid,
                json.dumps(list(record.registry_ids)),
                json.dumps(list(record.publication_types)),
                record.raw_hash,
            ),
        )


def _row_to_record(row: dict[str, Any]) -> SourceRecord:
    authors = row.get("authors")
    if isinstance(authors, str):
        authors = json.loads(authors)
    elif authors is None:
        authors = []

    reg_ids = row.get("registry_ids")
    if isinstance(reg_ids, str):
        reg_ids = json.loads(reg_ids)
    elif reg_ids is None:
        reg_ids = []

    pub_types = row.get("publication_types")
    if isinstance(pub_types, str):
        pub_types = json.loads(pub_types)
    elif pub_types is None:
        pub_types = []

    return SourceRecord(
        occurrence_id=row["occurrence_id"],
        source=row["source"],
        provider_record_id=row["provider_record_id"],
        ordinal=row["ordinal"],
        title=row["title"],
        abstract=row.get("abstract"),
        authors=tuple(authors),
        year=row.get("year"),
        journal=row.get("journal"),
        doi=row.get("doi"),
        pmid=row.get("pmid"),
        registry_ids=tuple(reg_ids),
        publication_types=tuple(pub_types),
        raw_hash=row["raw_hash"],
    )


def _row_to_execution(row: dict[str, Any], records: tuple[SourceRecord, ...]) -> SearchExecution:
    limits = row.get("limits")
    if isinstance(limits, str):
        limits = json.loads(limits)
    elif limits is None:
        limits = []

    raw_hashes = row.get("raw_artifact_hashes")
    if isinstance(raw_hashes, str):
        raw_hashes = json.loads(raw_hashes)
    elif raw_hashes is None:
        raw_hashes = []

    plan = SearchPlan(
        topic_id=row["topic_id"],
        source=row["source"],
        platform=row["platform"],
        strategy=row["strategy"],
        limits=tuple(limits),
    )

    return SearchExecution(
        id=row["id"],
        plan=plan,
        kind=row["kind"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        query_translation=row.get("query_translation"),
        reported_count=row["reported_count"],
        retrieved_count=row["retrieved_count"],
        raw_artifact_hashes=tuple(raw_hashes),
        records=records,
    )


def get_execution(tx: Tx, project_id: Id, execution_id: Id) -> SearchExecution | None:
    """Retrieve an execution with its records by execution_id."""
    if _is_null_tx(tx):
        return None

    row = tx.execute(
        """
        select * from search_execution
        where id = %s and project_id = %s
        """,
        (execution_id, project_id),
    ).fetchone()

    if not row:
        return None

    record_rows = tx.execute(
        """
        select * from source_occurrence
        where execution_id = %s and project_id = %s
        order by ordinal asc
        """,
        (execution_id, project_id),
    ).fetchall()

    records = tuple(_row_to_record(r) for r in record_rows)
    return _row_to_execution(row, records)


def list_executions(
    tx: Tx, project_id: Id, *, topic_id: str | None = None
) -> list[SearchExecution]:
    """List executions for a project, optionally filtered by topic_id."""
    if _is_null_tx(tx):
        return []

    if topic_id:
        exec_rows = tx.execute(
            """
            select * from search_execution
            where project_id = %s and topic_id = %s
            order by created_at desc
            """,
            (project_id, topic_id),
        ).fetchall()
    else:
        exec_rows = tx.execute(
            """
            select * from search_execution
            where project_id = %s
            order by created_at desc
            """,
            (project_id,),
        ).fetchall()

    results: list[SearchExecution] = []
    for row in exec_rows:
        record_rows = tx.execute(
            """
            select * from source_occurrence
            where execution_id = %s and project_id = %s
            order by ordinal asc
            """,
            (row["id"], project_id),
        ).fetchall()
        records = tuple(_row_to_record(r) for r in record_rows)
        results.append(_row_to_execution(row, records))

    return results
