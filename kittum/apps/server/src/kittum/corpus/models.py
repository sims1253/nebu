"""Data models for corpus module."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from kittum.core import Id, KittumError, Record, new_id
from kittum.search import SourceRecord

type Relation = Literal["same_record", "same_report", "same_study"]
type LinkRelation = Literal["same_record", "same_report", "same_study", "standalone"]
type ProposalStatus = Literal["pending", "confirmed", "rejected"]
type SplitLevel = Literal["report", "study"]


class StaleRevision(KittumError):
    """A change was asked of an identity revision that is no longer the project's newest."""


class Occurrence(Record):
    id: Id  # equals `SourceRecord.occurrence_id`
    search_id: Id
    provider_record_id: str
    source: str = ""


class LinkProposal(Record):
    id: Id = Field(default_factory=new_id)
    occurrence_ids: tuple[Id, ...]
    relation: Relation
    evidence: tuple[str, ...]  # e.g. "Same NCT ID NCT01234567", "Titles 97% alike"
    status: ProposalStatus = "pending"
    exact: bool = False
    """A duplicate on a report identifier only (DOI, PMID, PMCID), which the lead may confirm
    with every other such duplicate at once."""


class IdentityRevision(Record):
    id: Id
    project_id: Id
    parent_id: Id | None = None
    event_seq: int = 0
    event_id: Id | None = None


class CorpusRevision(Record):
    id: Id
    project_id: Id
    occurrences: tuple[Occurrence, ...]
    proposals: tuple[LinkProposal, ...]
    identity_revision_id: Id | None = None


class IdentityLink(Record):
    id: Id
    revision_id: Id
    occurrence_id: Id
    report_id: Id
    study_id: Id
    relation: str
    source: str = ""


class PrismaCounts(Record):
    records_identified_per_source: dict[str, int]
    total_records_identified: int
    duplicates_removed: int
    unique_records: int
    studies_identified: int


class CorpusReport(Record):
    """One report of an identity revision with the source records that make it up.

    `records` are in source order; a report found by several searches has one record per
    occurrence. `search_ids` names the search each record came from, in the same order.
    `relation` is the strongest link label among them.
    """

    report_id: Id
    study_id: Id
    relation: LinkRelation
    records: tuple[SourceRecord, ...]
    search_ids: tuple[Id, ...] = ()


class CorpusSearch(Record):
    """A search whose records are in the corpus, as the corpus view names it."""

    id: Id
    source: str
    platform: str
    kind: Literal["live_api", "file_import"]
    started_at: datetime
    retrieved_count: int


type LinkedBy = Literal["confirmed", "merged_by_hand"]


class CorpusLink(Record):
    """Why records of an identity revision share a report or a study: a proposal the lead
    confirmed, with its evidence, or the lead's own merge, with the reason they gave.
    `occurrence_ids` are the link's records that are still together in the revision."""

    relation: Relation
    occurrence_ids: tuple[Id, ...]
    evidence: tuple[str, ...]
    linked_by: LinkedBy
    revision_id: Id
