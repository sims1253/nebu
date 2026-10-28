"""Records, reports, and studies (DESIGN.md, "Corpus and identities").

The study is the unit of inclusion. Kittum proposes which records belong to which report and
study; a human confirms the links. PRISMA counts come from a frozen identity revision.

Owner: WP-07. Shipped now: the types. Still to build: dedupe rules, link proposals (shared NCT
number, DOI family, title similarity), identity revisions in Postgres, and occurrence retention.
"""

from typing import TYPE_CHECKING, Literal

from kittum.core import Id, Record

if TYPE_CHECKING:
    from kittum.search import SearchExecution

type Relation = Literal["same_record", "same_report", "same_study"]


class Occurrence(Record):
    id: Id
    search_id: Id
    provider_record_id: str


class LinkProposal(Record):
    occurrence_ids: tuple[Id, ...]
    relation: Relation
    evidence: tuple[str, ...]  # e.g. "shared NCT01234567", "title similarity 0.97"


class IdentityRevision(Record):
    id: Id
    project_id: Id
    parent_id: Id | None
    event_seq: int


class CorpusRevision(Record):
    id: Id
    project_id: Id
    occurrences: tuple[Occurrence, ...]
    proposals: tuple[LinkProposal, ...]


def ingest(project_id: Id, executions: tuple[SearchExecution, ...]) -> CorpusRevision:
    raise NotImplementedError("WP-07")


def link(proposal: LinkProposal, *, confirmed_by: Id) -> IdentityRevision:
    raise NotImplementedError("WP-07")


__all__ = [
    "CorpusRevision",
    "IdentityRevision",
    "LinkProposal",
    "Occurrence",
    "Relation",
    "ingest",
    "link",
]
