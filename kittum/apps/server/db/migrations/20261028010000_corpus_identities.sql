-- migrate:up

create table report (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    created_at timestamptz not null default now()
);

create table study (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    created_at timestamptz not null default now()
);

create table corpus_revision (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    created_at timestamptz not null default now()
);

create table identity_revision (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    parent_id uuid references identity_revision (id),
    event_seq bigint not null default 0,
    event_id uuid,
    created_at timestamptz not null default now()
);

create table identity_link (
    id uuid primary key default uuidv7(),
    revision_id uuid not null references identity_revision (id),
    occurrence_id uuid not null,
    report_id uuid not null references report (id),
    study_id uuid not null references study (id),
    relation text not null default 'standalone' check (relation in ('same_record', 'same_report', 'same_study', 'standalone')),
    source text not null default ''
);

create index idx_identity_link_revision on identity_link (revision_id);
create index idx_identity_link_occurrence on identity_link (occurrence_id);
create index idx_identity_link_report on identity_link (report_id);
create index idx_identity_link_study on identity_link (study_id);

create table proposal (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    corpus_revision_id uuid references corpus_revision (id),
    relation text not null check (relation in ('same_record', 'same_report', 'same_study')),
    occurrence_ids uuid[] not null,
    evidence text[] not null,
    status text not null default 'pending' check (status in ('pending', 'confirmed', 'rejected')),
    created_at timestamptz not null default now()
);

create index idx_proposal_project on proposal (project_id);
create index idx_proposal_corpus_revision on proposal (corpus_revision_id);

create view proposals as select * from proposal;

-- migrate:down

drop view if exists proposals;
drop table if exists proposal;
drop table if exists identity_link;
drop table if exists identity_revision;
drop table if exists corpus_revision;
drop table if exists study;
drop table if exists report;
