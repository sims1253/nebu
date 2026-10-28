-- migrate:up

-- Extraction runs, the fields they review, both paths' proposals, and human acceptances
-- (DESIGN.md, "Extraction"). Anchors live in the documents module's `anchor` table. A run's
-- proposals never change; an acceptance is a new row with its own ledger event. Each row keeps
-- the ledger sequence number of the event that wrote it, so a bundle can export the state as of
-- a cutoff.

create table extraction_run (
    id uuid primary key,
    project_id uuid not null references project (id),
    command_id uuid not null unique,
    layer_id uuid not null references layer (id),
    document_id uuid not null references document (id),
    study_id uuid not null,
    report_id uuid not null,
    report jsonb not null,
    schema jsonb not null,
    schema_hash char(64) not null,
    agent jsonb,
    status text not null check (status in ('queued', 'succeeded', 'failed')),
    requested_by uuid not null,
    requested_seq bigint not null,
    publish_command_id uuid not null unique,
    janus_version text,
    model_identity text,
    transcript_hash char(64),
    output_hash char(64),
    recorded_seq bigint,
    created_at timestamptz not null,
    completed_at timestamptz,
    check ((status = 'succeeded') = (recorded_seq is not null))
);

create index extraction_run_by_project on extraction_run (project_id, created_at);

create table extraction_field (
    id uuid primary key,
    run_id uuid not null references extraction_run (id),
    project_id uuid not null references project (id),
    ordinal integer not null,
    key jsonb not null,
    key_hash char(64) not null,
    spec jsonb not null,
    kind text not null check (kind in ('count', 'decimal', 'percentage', 'ratio', 'p_value', 'text')),
    labels jsonb not null,
    agreement text not null
        check (agreement in ('agree', 'disagree', 'janus_only', 'llm_only', 'neither')),
    gate text not null check (gate in ('human_verify', 'group_accept', 'human_review')),
    second_extractor boolean not null,
    recheck boolean not null,
    recorded_seq bigint not null,
    unique (run_id, key_hash),
    unique (run_id, ordinal)
);

create table extraction_proposal (
    id uuid primary key,
    field_id uuid not null references extraction_field (id),
    run_id uuid not null references extraction_run (id),
    path text not null check (path in ('janus', 'llm')),
    value text,
    unit text,
    status text not null check (status in ('extracted', 'missing', 'ambiguous', 'invalid', 'error')),
    message text,
    quote text,
    anchor_ids uuid[] not null,
    validators jsonb not null,
    recorded_seq bigint not null,
    unique (field_id, path),
    check (status <> 'extracted' or (value is not null and cardinality(anchor_ids) = 1))
);

create table extraction_acceptance (
    id uuid primary key,
    field_id uuid not null unique references extraction_field (id),
    run_id uuid not null references extraction_run (id),
    project_id uuid not null references project (id),
    key_hash char(64) not null,
    path text not null check (path in ('janus', 'llm')),
    edited boolean not null,
    grouped boolean not null,
    value text,
    unit text,
    anchor_id uuid not null references anchor (id),
    reviewer_id uuid not null,
    source_viewed_ms integer not null check (source_viewed_ms >= 0),
    recheck boolean not null,
    accepted_at timestamptz not null,
    event_seq bigint not null
);

create index extraction_acceptance_by_key on extraction_acceptance (project_id, key_hash, event_seq);

-- migrate:down

drop table extraction_acceptance;
drop table extraction_proposal;
drop table extraction_field;
drop table extraction_run;
