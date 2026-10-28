-- migrate:up

create table search_execution (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    topic_id text not null,
    source text not null,
    platform text not null,
    kind text not null check (kind in ('live_api', 'file_import')),
    strategy text not null,
    limits jsonb not null default '[]'::jsonb,
    query_translation text,
    reported_count integer not null,
    retrieved_count integer not null,
    started_at timestamptz not null,
    ended_at timestamptz not null,
    raw_artifact_hashes jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    unique (id, project_id)
);

create index search_execution_project on search_execution (project_id, topic_id);

create table source_occurrence (
    occurrence_id uuid primary key default uuidv7(),
    execution_id uuid not null,
    project_id uuid not null references project (id),
    source text not null,
    provider_record_id text not null,
    ordinal integer not null,
    title text not null,
    abstract text,
    authors jsonb not null default '[]'::jsonb,
    year integer,
    journal text,
    doi text,
    pmid text,
    registry_ids jsonb not null default '[]'::jsonb,
    publication_types jsonb not null default '[]'::jsonb,
    raw_hash char(64) not null,
    created_at timestamptz not null default now(),
    -- An occurrence belongs to the same project as its execution.
    foreign key (execution_id, project_id)
        references search_execution (id, project_id) on delete cascade
);

create index source_occurrence_execution on source_occurrence (execution_id);
create index source_occurrence_project on source_occurrence (project_id, source);
create index source_occurrence_pmid on source_occurrence (project_id, pmid) where pmid is not null;
create index source_occurrence_doi on source_occurrence (project_id, doi) where doi is not null;

-- migrate:down

drop table source_occurrence;
drop table search_execution;
