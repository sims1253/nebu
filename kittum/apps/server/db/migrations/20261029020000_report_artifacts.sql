-- migrate:up

-- A rendered report target: its files are in the artifact store by hash, and `index_hash` is the
-- canonical JSON of the whole artifact. Rows are append-only; a new render is a new row.
create table report_artifact (
    id uuid primary key,
    project_id uuid not null references project (id),
    target text not null,
    scope_id text,
    cutoff_seq bigint not null check (cutoff_seq >= 0),
    identity_revision_id uuid references identity_revision (id),
    index_hash char(64) not null,
    files jsonb not null,
    actor_id uuid not null references actor (id),
    event_id uuid not null references audit_event (event_id),
    rendered_at timestamptz not null
);

create index report_artifact_project on report_artifact (project_id, rendered_at);
create index report_artifact_files on report_artifact using gin (files jsonb_path_ops);

create function report_artifact_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'report_artifact rows are append-only (% on %)', tg_op, old.id;
end;
$$;

create trigger report_artifact_append_only
    before update or delete on report_artifact
    for each row execute function report_artifact_is_append_only();

-- migrate:down

drop trigger report_artifact_append_only on report_artifact;
drop function report_artifact_is_append_only;
drop table report_artifact;
