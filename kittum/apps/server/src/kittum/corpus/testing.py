"""Test helpers and fakes for the corpus module."""

from typing import TYPE_CHECKING

from kittum.core import Id, NotFoundError, new_id
from kittum.corpus import (
    CorpusRevision,
    IdentityRevision,
    LinkProposal,
    Occurrence,
    PrismaCounts,
)
from kittum.corpus.dedupe import generate_link_proposals
from kittum.corpus.identity import merge_links
from kittum.ledger import EventDraft, Ledger

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.search import SearchExecution


class FakeCorpus:
    """An in-memory fake for corpus operations, usable with NullTx."""

    def __init__(self, ledger: Ledger | None = None) -> None:
        self._ledger = ledger
        self.proposals: dict[Id, LinkProposal] = {}
        self.revisions: dict[Id, IdentityRevision] = {}
        # revision_id -> list of (occurrence_id, report_id, study_id, relation, source)
        self.links: dict[Id, list[dict[str, object]]] = {}

    def ingest(
        self,
        tx: Tx,
        project_id: Id,
        executions: tuple[SearchExecution, ...] | list[SearchExecution],
        *,
        actor_id: Id | None = None,
        ledger: Ledger | None = None,
    ) -> CorpusRevision:
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

        for p in proposals:
            self.proposals[p.id] = p

        initial_links: list[dict[str, object]] = []
        for occ in occurrences:
            initial_links.append(
                {
                    "id": new_id(),
                    "occurrence_id": occ.id,
                    "report_id": new_id(),
                    "study_id": new_id(),
                    "relation": "standalone",
                    "source": occ.source,
                }
            )
        self.links[identity_rev_id] = initial_links
        self.revisions[identity_rev_id] = IdentityRevision(
            id=identity_rev_id, project_id=project_id, parent_id=None
        )

        return CorpusRevision(
            id=corpus_rev_id,
            project_id=project_id,
            occurrences=tuple(occurrences),
            proposals=tuple(proposals),
            identity_revision_id=identity_rev_id,
        )

    def confirm(
        self,
        tx: Tx,
        project_id: Id,
        parent_revision_id: Id,
        proposal_id: Id,
        confirmed_by: Id,
        *,
        ledger: Ledger | None = None,
    ) -> IdentityRevision:
        prop = self.proposals.get(proposal_id)
        if not prop:
            raise NotFoundError(f"Proposal {proposal_id} not found")

        current_links = self._links(project_id, parent_revision_id)
        merged = merge_links(current_links, prop.occurrence_ids, prop.relation)

        new_rev_id = new_id()
        effective_ledger = ledger or self._ledger
        seq = 0
        event_id = None
        if effective_ledger is not None:
            event = effective_ledger.append(
                tx,
                EventDraft(
                    project_id=project_id,
                    type="corpus.confirmed",
                    schema_version=1,
                    actor_id=confirmed_by,
                    payload={
                        "revision_id": str(new_rev_id),
                        "parent_revision_id": str(parent_revision_id),
                        "proposal_id": str(proposal_id),
                        "relation": prop.relation,
                        "occurrence_ids": [str(oid) for oid in prop.occurrence_ids],
                    },
                ),
            )
            seq = event.seq
            event_id = event.event_id

        new_links: list[dict[str, object]] = [{"id": new_id(), **row} for row in merged]
        self.links[new_rev_id] = new_links
        rev = IdentityRevision(
            id=new_rev_id,
            project_id=project_id,
            parent_id=parent_revision_id,
            event_seq=seq,
            event_id=event_id,
        )
        self.revisions[new_rev_id] = rev
        return rev

    def get_counts(self, tx: Tx, project_id: Id, revision_id: Id) -> PrismaCounts:
        current_links = self._links(project_id, revision_id)
        per_source: dict[str, int] = {}
        for row in current_links:
            src = str(row["source"] or "unknown")
            per_source[src] = per_source.get(src, 0) + 1

        total = sum(per_source.values())
        unique_reps = len({row["report_id"] for row in current_links})
        unique_studies = len({row["study_id"] for row in current_links})

        return PrismaCounts(
            records_identified_per_source=per_source,
            total_records_identified=total,
            duplicates_removed=total - unique_reps,
            unique_records=unique_reps,
            studies_identified=unique_studies,
        )

    def _links(self, project_id: Id, revision_id: Id) -> list[dict[str, object]]:
        revision = self.revisions.get(revision_id)
        if revision is None or revision.project_id != project_id:
            raise NotFoundError(
                f"Identity revision {revision_id} not found in project {project_id}"
            )
        return self.links[revision_id]
