-- migrate:up

-- One enrichment per source occurrence. Re-enrichment after an update search replaces the row.
-- Enrichment never adds PRISMA identification counts; it only fills fields on existing records.

create table record_enrichment (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    occurrence_id uuid not null,
    provider_record_id text not null,
    doi text,
    pmid text,
    pmcid text,
    abstract text,
    retracted boolean not null default false,
    retraction_notice_doi text,
    oa_locations jsonb not null default '[]'::jsonb,
    entities jsonb not null default '[]'::jsonb,
    updates jsonb not null default '[]'::jsonb,
    clinical_trial_numbers jsonb not null default '[]'::jsonb,
    field_sources jsonb not null default '[]'::jsonb,
    enriched_at timestamptz not null default now(),
    unique (project_id, occurrence_id)
);

create index record_enrichment_doi on record_enrichment (project_id, doi);
create index record_enrichment_pmid on record_enrichment (project_id, pmid);

-- migrate:down

drop table record_enrichment;
