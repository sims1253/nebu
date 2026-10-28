"""Records, reports, and studies (DESIGN.md, "Corpus and identities").

The study is the unit of inclusion. Kittum proposes which records belong to which report and
study; a human confirms the links. PRISMA counts come from a frozen identity revision.

Owner: WP-07.
"""

from collections import defaultdict
from typing import TYPE_CHECKING, Any
from uuid import UUID

from kittum.core import Id, NotFoundError, new_id
from kittum.corpus.dedupe import (
    generate_link_proposals,
    normalize_registry_id,
    registry_evidence,
)
from kittum.corpus.identity import merge_links, split_links
from kittum.corpus.models import (
    CorpusLink,
    CorpusReport,
    CorpusRevision,
    CorpusSearch,
    IdentityLink,
    IdentityRevision,
    LinkedBy,
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
    from collections.abc import Iterable, Mapping

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
                    id, project_id, corpus_revision_id, relation, occurrence_ids, evidence, status,
                    exact
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    prop.id,
                    project_id,
                    corpus_rev_id,
                    prop.relation,
                    list(prop.occurrence_ids),
                    list(prop.evidence),
                    prop.status,
                    prop.exact,
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


def propose_study_links(
    tx: Tx,
    project_id: Id,
    registry_ids: Mapping[Id, Iterable[str]],
    *,
    found_by: str,
    actor_id: Id,
    ledger: Ledger,
) -> list[LinkProposal]:
    """Study-link proposals from registry IDs found after the corpus was built, such as the
    `clinical-trial-number` Crossref holds for a paper (`found_by` names the source). A record
    gets a proposal with every record of the newest identity revision that names the same ID,
    from its search or from here, unless they are one study already or an earlier proposal, of
    any status, joins their studies.
    Writes a `corpus.proposed` event when there is a proposal."""
    revision = latest_revision(tx, project_id)
    corpus_revision = tx.execute(
        "select id from corpus_revision where project_id = %s order by created_at desc limit 1",
        (project_id,),
    ).fetchone()
    if revision is None or corpus_revision is None:
        return []
    by_id: dict[str, set[Id]] = defaultdict(set)
    from_here: dict[str, set[Id]] = defaultdict(set)
    study: dict[Id, Id] = {}
    for report in reports(tx, project_id, revision.id):
        for record in report.records:
            study[record.occurrence_id] = report.study_id
            for reg in record.registry_ids:
                by_id[normalize_registry_id(reg)].add(record.occurrence_id)
    for oid, ids in registry_ids.items():
        if oid in study:
            for reg in ids:
                if norm := normalize_registry_id(reg):
                    by_id[norm].add(oid)
                    from_here[norm].add(oid)
    # The studies each earlier proposal would join, as of now; a proposal that joins no study
    # beyond one of those adds nothing for the lead.
    known = [
        {study[o] for o in p.occurrence_ids if o in study}
        for p in list_proposals(tx, project_id)
        if p.relation == "same_study"
    ]
    proposals = [
        LinkProposal(
            occurrence_ids=tuple(sorted(oids)),
            relation="same_study",
            evidence=(f"{registry_evidence(reg)} (from {found_by})",),
        )
        for reg, oids in sorted(by_id.items())
        if from_here[reg]
        and len(studies := {study[o] for o in oids}) >= 2
        and not any(studies <= k for k in known)
    ]
    if not proposals:
        return []
    ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type="corpus.proposed",
            schema_version=1,
            actor_id=actor_id,
            payload={
                "found_by": found_by,
                "proposals": [
                    {"id": str(p.id), "occurrence_ids": [str(o) for o in p.occurrence_ids]}
                    for p in proposals
                ],
            },
        ),
    )
    for prop in proposals:
        tx.execute(
            """
            insert into proposal (
                id, project_id, corpus_revision_id, relation, occurrence_ids, evidence, status
            ) values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                prop.id,
                project_id,
                corpus_revision["id"],
                prop.relation,
                list(prop.occurrence_ids),
                list(prop.evidence),
                prop.status,
            ),
        )
    return proposals


def confirm_exact(
    tx: Tx,
    project_id: Id,
    parent_revision_id: Id,
    confirmed_by: Id,
    *,
    ledger: Ledger,
) -> IdentityRevision:
    """Confirm every open proposal of a duplicate on a report identifier (same DOI, PMID, or
    PMCID), one `corpus.confirmed` event and identity revision each, as `confirm` does. Returns
    the newest revision. Raises `ValueError` when there is none to confirm."""
    pending = [
        p
        for p in list_proposals(tx, project_id, status="pending")
        if p.exact and p.relation == "same_record"
    ]
    if not pending:
        raise ValueError("no open duplicate shares a DOI, PMID, or PMCID")
    revision_id = parent_revision_id
    revision: IdentityRevision | None = None
    for proposal in pending:
        revision = confirm(tx, project_id, revision_id, proposal.id, confirmed_by, ledger=ledger)
        revision_id = revision.id
    assert revision is not None
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
        sql = (
            "select id, occurrence_ids, relation, evidence, status, exact from proposal"
            " where project_id = %s"
        )
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
                exact=row["exact"],
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
    occurrence (search order, then the record's place in its search), and the search of each."""
    with tx.cursor() as cur:
        links = _parent_links(cur, project_id, revision_id)
    executions = list(reversed(list_executions(tx, project_id)))  # newest first, so oldest first
    records = {r.occurrence_id: r for e in executions for r in e.records}
    found_in = {r.occurrence_id: e.id for e in executions for r in e.records}
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
            search_ids=tuple(found_in[r["occurrence_id"]] for r in rows),
        )
        for report_id, rows in grouped.items()
    ]


def searches(tx: Tx, project_id: Id) -> list[CorpusSearch]:
    """The project's searches, oldest first, as the corpus view names them."""
    rows = tx.execute(
        "select id, source, platform, kind, started_at, retrieved_count from search_execution"
        " where project_id = %s order by created_at, started_at",
        (project_id,),
    ).fetchall()
    return [CorpusSearch.model_validate(row) for row in rows]


def links(tx: Tx, project_id: Id, revision_id: Id, *, ledger: Ledger) -> list[CorpusLink]:
    """Why the records of an identity revision that share a report or a study are together: each
    confirmation and merge by hand on the way to the revision whose records are still together.
    A split undoes, for good, every earlier link it took records out of: a later merge of the same
    records shows only its own reason. A link whose records were split in part keeps the records
    still together, if they still make it: two records of a report, or records of two reports of
    a study."""
    with tx.cursor() as cur:
        chain: list[dict[str, Any]] = []
        at: Id | None = revision_id
        while at is not None:
            cur.execute(
                "select id, parent_id, event_seq from identity_revision"
                " where id = %s and project_id = %s",
                (at, project_id),
            )
            row = cur.fetchone()
            if row is None:
                break
            chain.append(row)
            at = row["parent_id"]
        evidence = {
            row["id"]: tuple(row["evidence"])
            for row in cur.execute(
                "select id, evidence from proposal where project_id = %s and status = 'confirmed'",
                (project_id,),
            ).fetchall()
        }
    found: list[CorpusLink] = []
    for revision in reversed(chain):
        events = ledger.events(
            tx, project_id, after_seq=revision["event_seq"] - 1, through_seq=revision["event_seq"]
        )
        event = events[0] if events else None
        if event is None:
            continue
        payload = event.payload
        if event.type == "corpus.split":
            found = _still_together(found, _links_by_occurrence(tx, project_id, revision["id"]))
            continue
        if event.type not in ("corpus.confirmed", "corpus.merged"):
            continue
        linked_by: LinkedBy
        if event.type == "corpus.confirmed":
            linked_by = "confirmed"
            reasons = evidence.get(UUID(payload["proposal_id"]), ())
        else:
            linked_by = "merged_by_hand"
            reasons = (payload["reason"],) if payload.get("reason") else ()
        found.append(
            CorpusLink(
                relation=payload["relation"],
                occurrence_ids=tuple(UUID(o) for o in payload["occurrence_ids"]),
                evidence=reasons,
                linked_by=linked_by,
                revision_id=revision["id"],
            )
        )
    return _still_together(found, _links_by_occurrence(tx, project_id, revision_id))


def _links_by_occurrence(tx: Tx, project_id: Id, revision_id: Id) -> dict[Id, dict[str, Any]]:
    with tx.cursor() as cur:
        return {row["occurrence_id"]: row for row in _parent_links(cur, project_id, revision_id)}


def _still_together(
    found: list[CorpusLink], placed: Mapping[Id, Mapping[str, Any]]
) -> list[CorpusLink]:
    """Each link cut down to its records that `placed` keeps together, as groups that still make
    the link: two records of a report, or records of two reports of a study."""
    kept: list[CorpusLink] = []
    for link in found:
        level = "study_id" if link.relation == "same_study" else "report_id"
        together: dict[Id, list[Id]] = {}
        for oid in link.occurrence_ids:
            if oid in placed:
                together.setdefault(placed[oid][level], []).append(oid)
        kept += [
            link.model_copy(update={"occurrence_ids": tuple(group)})
            for group in together.values()
            if len({placed[oid]["report_id"] for oid in group} if level == "study_id" else group)
            >= 2
        ]
    return kept


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
    "CorpusLink",
    "CorpusReport",
    "CorpusRevision",
    "CorpusSearch",
    "IdentityLink",
    "IdentityRevision",
    "LinkProposal",
    "LinkRelation",
    "LinkedBy",
    "Occurrence",
    "PrismaCounts",
    "ProposalStatus",
    "Relation",
    "SplitLevel",
    "confirm",
    "confirm_exact",
    "get_counts",
    "ingest",
    "latest_revision",
    "link",
    "links",
    "list_proposals",
    "merge",
    "propose_study_links",
    "reports",
    "searches",
    "split",
]
