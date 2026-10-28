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
    finalized boolean not null default false,
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

-- A revision is written in one transaction with all its links and then marked finalized. From
-- then on neither the revision nor its links change, so PRISMA counts from it stay reproducible.
create function identity_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception '% is append-only (%)', tg_table_name, tg_op;
end;
$$;

create function identity_revision_only_finalizes() returns trigger
language plpgsql as $$
begin
    if tg_op = 'UPDATE' and not old.finalized and new.finalized
        and (new.id, new.project_id, new.parent_id, new.event_seq, new.event_id, new.created_at)
            is not distinct from
            (old.id, old.project_id, old.parent_id, old.event_seq, old.event_id, old.created_at)
    then
        return new;
    end if;
    raise exception 'identity_revision % is immutable (%)', old.id, tg_op;
end;
$$;

create function identity_revision_is_finalized() returns trigger
language plpgsql as $$
begin
    if not exists (select 1 from identity_revision where id = new.id and finalized) then
        raise exception 'identity_revision % was not finalized in its transaction', new.id;
    end if;
    return null;
end;
$$;

create function identity_link_revision_is_open() returns trigger
language plpgsql as $$
begin
    if not exists (
        select 1 from identity_revision where id = new.revision_id and not finalized
    ) then
        raise exception 'identity_revision % is finalized or not visible', new.revision_id;
    end if;
    return new;
end;
$$;

create trigger identity_revision_frozen
    before update or delete on identity_revision
    for each row execute function identity_revision_only_finalizes();

create trigger identity_revision_no_truncate
    before truncate on identity_revision
    for each statement execute function identity_is_append_only();

create constraint trigger identity_revision_finalized
    after insert on identity_revision
    deferrable initially deferred
    for each row execute function identity_revision_is_finalized();

create trigger identity_link_frozen
    before update or delete on identity_link
    for each row execute function identity_is_append_only();

create trigger identity_link_no_truncate
    before truncate on identity_link
    for each statement execute function identity_is_append_only();

create trigger identity_link_open_revision
    before insert on identity_link
    for each row execute function identity_link_revision_is_open();

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
drop function if exists identity_link_revision_is_open();
drop function if exists identity_revision_is_finalized();
drop function if exists identity_revision_only_finalizes();
drop function if exists identity_is_append_only();
drop table if exists corpus_revision;
drop table if exists study;
drop table if exists report;
