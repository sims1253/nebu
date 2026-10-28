"""Rows for enrichment, one per source occurrence.

The same occurrence can be enriched again after an update search, so the row is replaced rather
than duplicated.
"""

from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Tx
    from kittum.enrich.types import RecordEnrichment

_SQL = """
insert into record_enrichment (
    project_id, occurrence_id, provider_record_id, doi, pmid, pmcid, abstract,
    retracted, retraction_notice_doi, oa_locations, entities, updates,
    clinical_trial_numbers, field_sources
) values (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
on conflict (project_id, occurrence_id) do update set
    provider_record_id = excluded.provider_record_id,
    doi = excluded.doi,
    pmid = excluded.pmid,
    pmcid = excluded.pmcid,
    abstract = excluded.abstract,
    retracted = excluded.retracted,
    retraction_notice_doi = excluded.retraction_notice_doi,
    oa_locations = excluded.oa_locations,
    entities = excluded.entities,
    updates = excluded.updates,
    clinical_trial_numbers = excluded.clinical_trial_numbers,
    field_sources = excluded.field_sources,
    enriched_at = now()
"""


def _json(record: object) -> Jsonb:
    return Jsonb(_dump(record))


def _dump(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        return dump(mode="json")
    if isinstance(value, tuple | list):
        return [_dump(item) for item in value]
    return value


def store(tx: Tx, project_id: Id, records: tuple[RecordEnrichment, ...]) -> None:
    for record in records:
        tx.execute(
            _SQL,
            (
                project_id,
                record.occurrence_id,
                record.provider_record_id,
                record.doi,
                record.pmid,
                record.pmcid,
                record.abstract,
                record.retracted,
                record.retraction_notice_doi,
                _json(record.oa_locations),
                _json(record.entities),
                _json(record.updates),
                _json(record.clinical_trial_numbers),
                _json(record.field_sources),
            ),
        )
