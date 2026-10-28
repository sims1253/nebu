"""The values `kittum.enrich` passes to other modules.

Kept in a submodule so the source clients can use them without a circular import; the package
re-exports every one of them from `kittum.enrich`.
"""

from datetime import date
from typing import Literal

from kittum.core import Id, Record

SourceFormat = Literal["pdf", "xml", "landing"]
OaVersion = Literal["published", "accepted", "submitted"]


class OaLocation(Record):
    url: str
    licence: str | None = None
    licence_start: date | None = None
    """When the licence takes effect, where the source states it (Crossref does)."""
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


FailureKind = Literal["http_error", "unreachable", "budget", "malformed", "other_work"]
"""Why a source failed: it answered with an error status, could not be reached, had no requests
left today, answered with something Kittum cannot read, or answered for another work."""

RETRYABLE: frozenset[FailureKind] = frozenset({"http_error", "unreachable", "budget", "malformed"})
"""The failures that asking again can mend. An answer for another work stays one."""


class SourceFailure(Record):
    source: str
    reason: str
    kind: FailureKind = "http_error"
    status: int | None = None
    """The last HTTP status, when the source answered at all."""
    message: str | None = None
    """What the source said about the failure, in its own words."""


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
    sources_asked: tuple[str, ...] = ()
    """The enrichment sources asked about this record."""
    failures: tuple[SourceFailure, ...] = ()
    """The sources that failed for this record, and why: an error, no budget, or an answer that
    names another work."""


class Enrichment(Record):
    records: tuple[RecordEnrichment, ...]
    failures: tuple[SourceFailure, ...] = ()


class FullTextCandidate(Record):
    url: str
    format: Literal["pdf", "xml"]
    licence: str | None = None
    licence_start: date | None = None
    version: OaVersion | None = None
    source: str


class EnrichmentConfig(Record):
    """What the source clients need. Entry points map `kittum.settings.Settings` onto this."""

    contact_email: str | None = None
    openalex_api_key: str | None = None
    semantic_scholar_api_key: str | None = None
    core_api_key: str | None = None
    """CORE answers only with a key; without one the waterfall skips it."""
    batch_size: int = 50


class WorkIds(Record):
    """What the full-text lookups know a work by."""

    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None


class FullTextLookup(Record):
    """What one full-text source said about a work: the request that asked it, and the file
    locations it named (none when it has no open copy)."""

    source: str
    url: str
    locations: tuple[OaLocation, ...] = ()
