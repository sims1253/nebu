"""Data models for corpus module."""

from typing import Literal

from pydantic import Field

from kittum.core import Id, Record, new_id

type Relation = Literal["same_record", "same_report", "same_study"]
type LinkRelation = Literal["same_record", "same_report", "same_study", "standalone"]
type ProposalStatus = Literal["pending", "confirmed", "rejected"]
type SplitLevel = Literal["report", "study"]


class Occurrence(Record):
    id: Id  # equals `SourceRecord.occurrence_id`
    search_id: Id
    provider_record_id: str
    source: str = ""


class LinkProposal(Record):
    id: Id = Field(default_factory=new_id)
    occurrence_ids: tuple[Id, ...]
    relation: Relation
    evidence: tuple[str, ...]  # e.g. "shared NCT01234567", "title similarity 0.97"
    status: ProposalStatus = "pending"


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
