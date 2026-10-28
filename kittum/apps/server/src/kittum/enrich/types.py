"""The values `kittum.enrich` passes to other modules.

Kept in a submodule so the source clients can use them without a circular import; the package
re-exports every one of them from `kittum.enrich`.
"""

from typing import Literal

from kittum.core import Id, Record

SourceFormat = Literal["pdf", "xml", "landing"]
OaVersion = Literal["published", "accepted", "submitted"]


class OaLocation(Record):
    url: str
    licence: str | None = None
    version: OaVersion | None = None
    source: str = ""
    format: SourceFormat | None = None


class EntityTag(Record):
    start: int
    end: int
    label: str  # e.g. "drug", "disease"
    normalized_id: str | None = None
    tagger: str


class WorkUpdate(Record):
    """A retraction, correction, or erratum that Crossref links to a work."""

    doi: str
    kind: str  # "retraction", "correction", "erratum", "expression_of_concern", ...
    direction: Literal["updates", "updated_by"]
    """`updates`: this work updates the linked work. `updated_by`: the linked work updates this."""


class FieldSource(Record):
    """Which source supplied one field, so a reviewer can see where a value came from."""

    field: str
    source: str


class RecordEnrichment(Record):
    occurrence_id: Id
    provider_record_id: str
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract: str | None = None
    retracted: bool = False
    retraction_notice_doi: str | None = None
    oa_locations: tuple[OaLocation, ...] = ()
    entities: tuple[EntityTag, ...] = ()
    updates: tuple[WorkUpdate, ...] = ()
    clinical_trial_numbers: tuple[str, ...] = ()
    field_sources: tuple[FieldSource, ...] = ()
    sources_used: tuple[str, ...] = ()


class SourceFailure(Record):
    source: str
    reason: str


class Enrichment(Record):
    records: tuple[RecordEnrichment, ...]
    failures: tuple[SourceFailure, ...] = ()


class FullTextCandidate(Record):
    url: str
    format: Literal["pdf", "xml"]
    licence: str | None = None
    version: OaVersion | None = None
    source: str


class EnrichmentConfig(Record):
    """What the source clients need. Entry points map `kittum.settings.Settings` onto this."""

    contact_email: str | None = None
    openalex_api_key: str | None = None
    batch_size: int = 50
