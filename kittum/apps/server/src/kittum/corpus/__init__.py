"""Records, reports, and studies (DESIGN.md, "Corpus and identities").

The study is the unit of inclusion. Kittum proposes which records belong to which report and
study; a human confirms the links. PRISMA counts come from a frozen identity revision.

Owner: WP-07.
"""

from typing import TYPE_CHECKING, Any

from kittum.core import Id, NotFoundError, new_id
from kittum.corpus.dedupe import generate_link_proposals
from kittum.corpus.identity import merge_links, split_links
from kittum.corpus.models import (
    CorpusReport,
    CorpusRevision,
    IdentityLink,
    IdentityRevision,
    LinkProposal,
    LinkRelation,
    Occurrence,
    PrismaCounts,
    ProposalStatus,
    Relation,
    SplitLevel,
)
from kittum.ledger import EventDraft, Ledger
from kittum.search import list_executions

if TYPE_CHECKING:
    from psycopg import Cursor

    from kittum.db import Tx
    from kittum.ledger import AuditEvent
    from kittum.search import SearchExecution


def ingest(
    tx: Tx,
    project_id: Id,
    executions: tuple[SearchExecution, ...] | list[SearchExecution],
    *,
    actor_id: Id,
    ledger: Ledger,
) -> CorpusRevision:
    """Ingest search executions, generate link proposals, and create the first identity revision.

    Writes a `corpus.ingested` ledger event in the caller's transaction.
    """
    all_records = []
    occurrences: list[Occurrence] = []
    for execution in executions:
        for rec in execution.records:
            all_records.append(rec)
            occurrences.append(
                Occurrence(
                    id=rec.occurrence_id,
                    search_id=execution.id,
                    provider_record_id=rec.provider_record_id,
                    source=rec.source,
                )
            )

    proposals = generate_link_proposals(all_records)
    corpus_rev_id = new_id()
    identity_rev_id = new_id()

    event = ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type="corpus.ingested",
            schema_version=1,
            actor_id=actor_id,
            payload={
                "corpus_revision_id": str(corpus_rev_id),
                "identity_revision_id": str(identity_rev_id),
                "search_ids": [str(e.id) for e in executions],
                "occurrences": len(occurrences),
                "proposals": len(proposals),
            },
        ),
    )
    with tx.cursor() as cur:
        cur.execute(
            "insert into corpus_revision (id, project_id) values (%s, %s)",
            (corpus_rev_id, project_id),
        )
        for prop in proposals:
            cur.execute(
                """
                insert into proposal (
                    id, project_id, corpus_revision_id, relation, occurrence_ids, evidence, status
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    prop.id,
                    project_id,
                    corpus_rev_id,
                    prop.relation,
                    list(prop.occurrence_ids),
                    list(prop.evidence),
                    prop.status,
                ),
            )
        links = [
            {
                "occurrence_id": occ.id,
                "report_id": new_id(),
                "study_id": new_id(),
                "relation": "standalone",
                "source": occ.source,
            }
            for occ in occurrences
        ]
        _write_revision(cur, project_id, identity_rev_id, None, event, [], links)

    return CorpusRevision(
        id=corpus_rev_id,
        project_id=project_id,
        occurrences=tuple(occurrences),
        proposals=tuple(proposals),
        identity_revision_id=identity_rev_id,
    )


def confirm(
    tx: Tx,
    project_id: Id,
    parent_revision_id: Id,
    proposal_id: Id,
    confirmed_by: Id,
    *,
    ledger: Ledger,
) -> IdentityRevision:
    """Human confirmation of a link proposal, creating a new identity revision and ledger event.

    The proposal's occurrences are merged with their complete groups (see `merge`).
    """
    with tx.cursor() as cur:
        cur.execute(
            "select * from proposal where id = %s and project_id = %s",
            (proposal_id, project_id),
        )
        prop_row = cur.fetchone()
        if not prop_row:
            raise NotFoundError(f"Proposal {proposal_id} not found")

        relation: Relation = prop_row["relation"]
        occurrence_ids: list[Id] = prop_row["occurrence_ids"]
        parent_links = _parent_links(cur, project_id, parent_revision_id)
        links = merge_links(parent_links, occurrence_ids, relation)

        new_revision_id = new_id()
        event = ledger.append(
            tx,
            EventDraft(
                project_id=project_id,
                type="corpus.confirmed",
                schema_version=1,
                actor_id=confirmed_by,
                payload={
                    "revision_id": str(new_revision_id),
                    "parent_revision_id": str(parent_revision_id),
                    "proposal_id": str(proposal_id),
                    "relation": relation,
                    "occurrence_ids": [str(oid) for oid in occurrence_ids],
                },
            ),
        )
        revision = _write_revision(
            cur, project_id, new_revision_id, parent_revision_id, event, parent_links, links
        )
        cur.execute(
            "update proposal set status = 'confirmed' where id = %s",
            (proposal_id,),
        )
    return revision


def merge(
    tx: Tx,
    project_id: Id,
    parent_revision_id: Id,
    occurrence_ids: tuple[Id, ...] | list[Id],
    relation: Relation,
    actor_id: Id,
    *,
    reason: str = "",
    ledger: Ledger,
) -> IdentityRevision:
    """Human manual merge of occurrences, creating a new identity revision and ledger event.

    `same_record` and `same_report` union the complete report groups of the occurrences (and so
    their studies); `same_study` unions their complete study groups and keeps the report groups.
    """
    with tx.cursor() as cur:
        parent_links = _parent_links(cur, project_id, parent_revision_id)
        links = merge_links(parent_links, occurrence_ids, relation)

        new_revision_id = new_id()
        event = ledger.append(
            tx,
            EventDraft(
                project_id=project_id,
                type="corpus.merged",
                schema_version=1,
                actor_id=actor_id,
                payload={
                    "revision_id": str(new_revision_id),
                    "parent_revision_id": str(parent_revision_id),
                    "relation": relation,
                    "occurrence_ids": [str(oid) for oid in occurrence_ids],
                    "reason": reason,
                },
            ),
        )
        return _write_revision(
            cur, project_id, new_revision_id, parent_revision_id, event, parent_links, links
        )


def split(
    tx: Tx,
    project_id: Id,
    parent_revision_id: Id,
    occurrence_ids: tuple[Id, ...] | list[Id],
    level: SplitLevel,
    actor_id: Id,
    *,
    reason: str = "",
    ledger: Ledger,
) -> IdentityRevision:
    """Human split of occurrences, undoing a previous merge without losing history.

    A `report` split moves each occurrence into a new report of its current study. A `study`
    split moves the complete report group of each occurrence into a new study.
    """
    with tx.cursor() as cur:
        parent_links = _parent_links(cur, project_id, parent_revision_id)
        links = split_links(parent_links, occurrence_ids, level)

        new_revision_id = new_id()
        event = ledger.append(
            tx,
            EventDraft(
                project_id=project_id,
                type="corpus.split",
                schema_version=1,
                actor_id=actor_id,
                payload={
                    "revision_id": str(new_revision_id),
                    "parent_revision_id": str(parent_revision_id),
                    "level": level,
                    "occurrence_ids": [str(oid) for oid in occurrence_ids],
                    "reason": reason,
                },
            ),
        )
        return _write_revision(
            cur, project_id, new_revision_id, parent_revision_id, event, parent_links, links
        )


def get_counts(tx: Tx, project_id: Id, revision_id: Id) -> PrismaCounts:
    """Calculate PRISMA 2020 identification counts from a frozen identity revision."""
    with tx.cursor() as cur:
        _require_revision(cur, project_id, revision_id)
        cur.execute(
            """
            select source, count(*) as cnt
            from identity_link
            where revision_id = %s
            group by source
            """,
            (revision_id,),
        )
        rows = cur.fetchall()
        per_source = {(row["source"] or "unknown"): int(row["cnt"]) for row in rows}
        total_records = sum(per_source.values())

        cur.execute(
            "select count(distinct report_id) as cnt from identity_link where revision_id = %s",
            (revision_id,),
        )
        row = cur.fetchone()
        unique_records = int(row["cnt"]) if row else 0

        cur.execute(
            "select count(distinct study_id) as cnt from identity_link where revision_id = %s",
            (revision_id,),
        )
        row = cur.fetchone()
        studies = int(row["cnt"]) if row else 0

        duplicates_removed = total_records - unique_records

        return PrismaCounts(
            records_identified_per_source=per_source,
            total_records_identified=total_records,
            duplicates_removed=duplicates_removed,
            unique_records=unique_records,
            studies_identified=studies,
        )


def list_proposals(
    tx: Tx,
    project_id: Id,
    *,
    corpus_revision_id: Id | None = None,
    status: ProposalStatus | None = None,
) -> list[LinkProposal]:
    """List proposals for a project."""
    with tx.cursor() as cur:
        sql = "select id, occurrence_ids, relation, evidence, status from proposal where project_id = %s"
        params: list[object] = [project_id]
        if corpus_revision_id is not None:
            sql += " and corpus_revision_id = %s"
            params.append(corpus_revision_id)
        if status is not None:
            sql += " and status = %s"
            params.append(status)
        sql += " order by created_at asc"
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [
            LinkProposal(
                id=row["id"],
                occurrence_ids=tuple(row["occurrence_ids"]),
                relation=row["relation"],
                evidence=tuple(row["evidence"]),
                status=row["status"],
            )
            for row in rows
        ]


def latest_revision(tx: Tx, project_id: Id) -> IdentityRevision | None:
    """The project's newest identity revision, the one every new decision reads."""
    row = tx.execute(
        "select id, project_id, parent_id, event_seq, event_id from identity_revision"
        " where project_id = %s and finalized order by event_seq desc limit 1",
        (project_id,),
    ).fetchone()
    return IdentityRevision.model_validate(row) if row else None


_STRONGEST = {"standalone": 0, "same_study": 1, "same_report": 2, "same_record": 3}


def reports(tx: Tx, project_id: Id, revision_id: Id) -> list[CorpusReport]:
    """Every report of an identity revision with its source records, in the order of the first
    occurrence (search order, then the record's place in its search)."""
    with tx.cursor() as cur:
        links = _parent_links(cur, project_id, revision_id)
    executions = reversed(list_executions(tx, project_id))  # newest first, so oldest first
    records = {r.occurrence_id: r for e in executions for r in e.records}
    order = {oid: i for i, oid in enumerate(records)}
    grouped: dict[Id, list[dict[str, Any]]] = {}
    for link_row in sorted(links, key=lambda row: order.get(row["occurrence_id"], len(order))):
        grouped.setdefault(link_row["report_id"], []).append(link_row)
    return [
        CorpusReport(
            report_id=report_id,
            study_id=rows[0]["study_id"],
            relation=max((r["relation"] for r in rows), key=_STRONGEST.__getitem__),
            records=tuple(records[r["occurrence_id"]] for r in rows),
        )
        for report_id, rows in grouped.items()
    ]


def link(
    proposal: LinkProposal,
    *,
    confirmed_by: Id,
    tx: Tx | None = None,
    project_id: Id | None = None,
    parent_revision_id: Id | None = None,
    ledger: Ledger,
) -> IdentityRevision:
    """Convenience alias for confirming a proposal."""
    if tx is None or project_id is None or parent_revision_id is None:
        raise ValueError("tx, project_id, and parent_revision_id are required")
    return confirm(
        tx,
        project_id,
        parent_revision_id,
        proposal.id,
        confirmed_by,
        ledger=ledger,
    )


def _require_revision(cur: Cursor[dict[str, Any]], project_id: Id, revision_id: Id) -> None:
    cur.execute(
        "select 1 from identity_revision where id = %s and project_id = %s",
        (revision_id, project_id),
    )
    if cur.fetchone() is None:
        raise NotFoundError(f"Identity revision {revision_id} not found in project {project_id}")


def _parent_links(
    cur: Cursor[dict[str, Any]], project_id: Id, revision_id: Id
) -> list[dict[str, Any]]:
    _require_revision(cur, project_id, revision_id)
    cur.execute(
        """
        select occurrence_id, report_id, study_id, relation, source
        from identity_link
        where revision_id = %s
        order by occurrence_id
        """,
        (revision_id,),
    )
    return cur.fetchall()


def _write_revision(
    cur: Cursor[dict[str, Any]],
    project_id: Id,
    revision_id: Id,
    parent_id: Id | None,
    event: AuditEvent,
    parent_links: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> IdentityRevision:
    """Write a revision with all its links, then mark it finalized; the database freezes it."""
    cur.execute(
        """
        insert into identity_revision (id, project_id, parent_id, event_seq, event_id)
        values (%s, %s, %s, %s, %s)
        """,
        (revision_id, project_id, parent_id, event.seq, event.event_id),
    )
    known_reports = {row["report_id"] for row in parent_links}
    known_studies = {row["study_id"] for row in parent_links}
    for report_id in dict.fromkeys(row["report_id"] for row in links):
        if report_id not in known_reports:
            cur.execute(
                "insert into report (id, project_id) values (%s, %s)", (report_id, project_id)
            )
    for study_id in dict.fromkeys(row["study_id"] for row in links):
        if study_id not in known_studies:
            cur.execute(
                "insert into study (id, project_id) values (%s, %s)", (study_id, project_id)
            )
    for row in links:
        cur.execute(
            """
            insert into identity_link (
                id, revision_id, occurrence_id, report_id, study_id, relation, source
            ) values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_id(),
                revision_id,
                row["occurrence_id"],
                row["report_id"],
                row["study_id"],
                row["relation"],
                row["source"],
            ),
        )
    cur.execute("update identity_revision set finalized = true where id = %s", (revision_id,))
    return IdentityRevision(
        id=revision_id,
        project_id=project_id,
        parent_id=parent_id,
        event_seq=event.seq,
        event_id=event.event_id,
    )


__all__ = [
    "CorpusReport",
    "CorpusRevision",
    "IdentityLink",
    "IdentityRevision",
    "LinkProposal",
    "LinkRelation",
    "Occurrence",
    "PrismaCounts",
    "ProposalStatus",
    "Relation",
    "SplitLevel",
    "confirm",
    "get_counts",
    "ingest",
    "latest_revision",
    "link",
    "list_proposals",
    "merge",
    "reports",
    "split",
]
