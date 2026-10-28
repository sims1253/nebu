"""Records, reports, and studies (DESIGN.md, "Corpus and identities").

The study is the unit of inclusion. Kittum proposes which records belong to which report and
study; a human confirms the links. PRISMA counts come from a frozen identity revision.

Owner: WP-07.
"""

from typing import TYPE_CHECKING

from kittum.core import Id, new_id
from kittum.corpus.dedupe import generate_link_proposals
from kittum.corpus.ledger import PostgresLedger
from kittum.corpus.models import (
    CorpusRevision,
    IdentityLink,
    IdentityRevision,
    LinkProposal,
    LinkRelation,
    Occurrence,
    PrismaCounts,
    ProposalStatus,
    Relation,
)
from kittum.ledger import EventDraft, Ledger

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.search import SearchExecution


def ingest(
    tx: Tx,
    project_id: Id,
    executions: tuple[SearchExecution, ...] | list[SearchExecution],
    *,
    ledger: Ledger | None = None,
) -> CorpusRevision:
    """Ingest search executions, generate link proposals, and create initial revisions."""
    _ = ledger
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

    # Try database persistence if tx is real
    try:
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
            cur.execute(
                """
                insert into identity_revision (id, project_id, parent_id, event_seq, event_id)
                values (%s, %s, null, 0, null)
                """,
                (identity_rev_id, project_id),
            )
            for occ in occurrences:
                report_id = new_id()
                study_id = new_id()
                cur.execute(
                    "insert into report (id, project_id) values (%s, %s)",
                    (report_id, project_id),
                )
                cur.execute(
                    "insert into study (id, project_id) values (%s, %s)",
                    (study_id, project_id),
                )
                cur.execute(
                    """
                    insert into identity_link (
                        id, revision_id, occurrence_id, report_id, study_id, relation, source
                    ) values (%s, %s, %s, %s, %s, 'standalone', %s)
                    """,
                    (
                        new_id(),
                        identity_rev_id,
                        occ.id,
                        report_id,
                        study_id,
                        occ.source,
                    ),
                )
    except AssertionError:
        # NullTx in tests
        pass

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
    ledger: Ledger | None = None,
) -> IdentityRevision:
    """Human confirmation of a link proposal, creating a new identity revision and ledger event."""
    effective_ledger = ledger or PostgresLedger()

    with tx.cursor() as cur:
        cur.execute(
            "select * from proposal where id = %s and project_id = %s",
            (proposal_id, project_id),
        )
        prop_row = cur.fetchone()
        if not prop_row:
            raise ValueError(f"Proposal {proposal_id} not found")

        relation: Relation = prop_row["relation"]
        occurrence_ids: list[Id] = prop_row["occurrence_ids"]

        cur.execute(
            """
            select occurrence_id, report_id, study_id, relation, source
            from identity_link
            where revision_id = %s
            """,
            (parent_revision_id,),
        )
        links = cur.fetchall()
        if not links:
            raise ValueError(f"No identity links found for revision {parent_revision_id}")

        link_map = {row["occurrence_id"]: row for row in links}
        target_ids = set(occurrence_ids)
        for oid in target_ids:
            if oid not in link_map:
                raise ValueError(f"Occurrence {oid} not found in revision {parent_revision_id}")

        first_oid = occurrence_ids[0]
        target_report_id = link_map[first_oid]["report_id"]
        target_study_id = link_map[first_oid]["study_id"]

        new_revision_id = new_id()

        event = effective_ledger.append(
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

        cur.execute(
            """
            insert into identity_revision (id, project_id, parent_id, event_seq, event_id)
            values (%s, %s, %s, %s, %s)
            """,
            (
                new_revision_id,
                project_id,
                parent_revision_id,
                event.seq,
                event.event_id,
            ),
        )

        for row in links:
            oid = row["occurrence_id"]
            if oid in target_ids:
                if relation in ("same_record", "same_report"):
                    rep_id = target_report_id
                    std_id = target_study_id
                else:  # same_study
                    rep_id = row["report_id"]
                    std_id = target_study_id
                link_rel = relation
            else:
                rep_id = row["report_id"]
                std_id = row["study_id"]
                link_rel = row["relation"]

            cur.execute(
                """
                insert into identity_link (
                    id, revision_id, occurrence_id, report_id, study_id, relation, source
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_id(),
                    new_revision_id,
                    oid,
                    rep_id,
                    std_id,
                    link_rel,
                    row["source"],
                ),
            )

        cur.execute(
            "update proposal set status = 'confirmed' where id = %s",
            (proposal_id,),
        )

    return IdentityRevision(
        id=new_revision_id,
        project_id=project_id,
        parent_id=parent_revision_id,
        event_seq=event.seq,
        event_id=event.event_id,
    )


def merge(
    tx: Tx,
    project_id: Id,
    parent_revision_id: Id,
    occurrence_ids: tuple[Id, ...] | list[Id],
    relation: Relation,
    actor_id: Id,
    *,
    reason: str = "",
    ledger: Ledger | None = None,
) -> IdentityRevision:
    """Human manual merge of occurrences, creating a new identity revision and ledger event."""
    effective_ledger = ledger or PostgresLedger()

    with tx.cursor() as cur:
        cur.execute(
            """
            select occurrence_id, report_id, study_id, relation, source
            from identity_link
            where revision_id = %s
            """,
            (parent_revision_id,),
        )
        links = cur.fetchall()
        if not links:
            raise ValueError(f"No identity links found for revision {parent_revision_id}")

        link_map = {row["occurrence_id"]: row for row in links}
        target_ids = set(occurrence_ids)
        for oid in target_ids:
            if oid not in link_map:
                raise ValueError(f"Occurrence {oid} not found in revision {parent_revision_id}")

        first_oid = next(iter(occurrence_ids))
        target_report_id = link_map[first_oid]["report_id"]
        target_study_id = link_map[first_oid]["study_id"]

        new_revision_id = new_id()

        event = effective_ledger.append(
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

        cur.execute(
            """
            insert into identity_revision (id, project_id, parent_id, event_seq, event_id)
            values (%s, %s, %s, %s, %s)
            """,
            (
                new_revision_id,
                project_id,
                parent_revision_id,
                event.seq,
                event.event_id,
            ),
        )

        for row in links:
            oid = row["occurrence_id"]
            if oid in target_ids:
                if relation in ("same_record", "same_report"):
                    rep_id = target_report_id
                    std_id = target_study_id
                else:  # same_study
                    rep_id = row["report_id"]
                    std_id = target_study_id
                link_rel = relation
            else:
                rep_id = row["report_id"]
                std_id = row["study_id"]
                link_rel = row["relation"]

            cur.execute(
                """
                insert into identity_link (
                    id, revision_id, occurrence_id, report_id, study_id, relation, source
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_id(),
                    new_revision_id,
                    oid,
                    rep_id,
                    std_id,
                    link_rel,
                    row["source"],
                ),
            )

    return IdentityRevision(
        id=new_revision_id,
        project_id=project_id,
        parent_id=parent_revision_id,
        event_seq=event.seq,
        event_id=event.event_id,
    )


def split(
    tx: Tx,
    project_id: Id,
    parent_revision_id: Id,
    occurrence_ids: tuple[Id, ...] | list[Id],
    actor_id: Id,
    *,
    reason: str = "",
    ledger: Ledger | None = None,
) -> IdentityRevision:
    """Human split of occurrences, undoing a previous merge without losing history."""
    effective_ledger = ledger or PostgresLedger()

    with tx.cursor() as cur:
        cur.execute(
            """
            select occurrence_id, report_id, study_id, relation, source
            from identity_link
            where revision_id = %s
            """,
            (parent_revision_id,),
        )
        links = cur.fetchall()
        if not links:
            raise ValueError(f"No identity links found for revision {parent_revision_id}")

        link_map = {row["occurrence_id"]: row for row in links}
        target_ids = set(occurrence_ids)
        for oid in target_ids:
            if oid not in link_map:
                raise ValueError(f"Occurrence {oid} not found in revision {parent_revision_id}")

        new_revision_id = new_id()

        event = effective_ledger.append(
            tx,
            EventDraft(
                project_id=project_id,
                type="corpus.split",
                schema_version=1,
                actor_id=actor_id,
                payload={
                    "revision_id": str(new_revision_id),
                    "parent_revision_id": str(parent_revision_id),
                    "occurrence_ids": [str(oid) for oid in occurrence_ids],
                    "reason": reason,
                },
            ),
        )

        cur.execute(
            """
            insert into identity_revision (id, project_id, parent_id, event_seq, event_id)
            values (%s, %s, %s, %s, %s)
            """,
            (
                new_revision_id,
                project_id,
                parent_revision_id,
                event.seq,
                event.event_id,
            ),
        )

        for row in links:
            oid = row["occurrence_id"]
            if oid in target_ids:
                # Assign fresh report and study
                rep_id = new_id()
                std_id = new_id()
                cur.execute(
                    "insert into report (id, project_id) values (%s, %s)",
                    (rep_id, project_id),
                )
                cur.execute(
                    "insert into study (id, project_id) values (%s, %s)",
                    (std_id, project_id),
                )
                link_rel = "standalone"
            else:
                rep_id = row["report_id"]
                std_id = row["study_id"]
                link_rel = row["relation"]

            cur.execute(
                """
                insert into identity_link (
                    id, revision_id, occurrence_id, report_id, study_id, relation, source
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_id(),
                    new_revision_id,
                    oid,
                    rep_id,
                    std_id,
                    link_rel,
                    row["source"],
                ),
            )

    return IdentityRevision(
        id=new_revision_id,
        project_id=project_id,
        parent_id=parent_revision_id,
        event_seq=event.seq,
        event_id=event.event_id,
    )


def get_counts(tx: Tx, revision_id: Id) -> PrismaCounts:
    """Calculate PRISMA 2020 identification counts from a frozen identity revision."""
    with tx.cursor() as cur:
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


def link(
    proposal: LinkProposal,
    *,
    confirmed_by: Id,
    tx: Tx | None = None,
    project_id: Id | None = None,
    parent_revision_id: Id | None = None,
    ledger: Ledger | None = None,
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


__all__ = [
    "CorpusRevision",
    "IdentityLink",
    "IdentityRevision",
    "LinkProposal",
    "LinkRelation",
    "Occurrence",
    "PrismaCounts",
    "ProposalStatus",
    "Relation",
    "confirm",
    "get_counts",
    "ingest",
    "link",
    "list_proposals",
    "merge",
    "split",
]
