-- migrate:up

-- Retrying the sources that failed, and uploading many PDFs at once.
--
-- An enrichment run can cover some records only (`occurrence_ids`, null for every record of the
-- revision), and a retry run names the reports whose open-access retrieval follows it once it is
-- done (`retrieve_after`). A retrieval run says what asked for it and which lookup sources its
-- worker left out, with the reason (`skipped`, `[{source, reason}]`).
alter table enrichment_run drop constraint enrichment_run_trigger_check;
alter table enrichment_run add constraint enrichment_run_trigger_check
    check (trigger in ('corpus_built', 'requested', 'retry'));
alter table enrichment_run add column occurrence_ids jsonb;
alter table enrichment_run add column retrieve_after jsonb;

alter table retrieval_run add column trigger text not null default 'requested'
    check (trigger in ('requested', 'retry'));
alter table retrieval_run add column skipped jsonb not null default '[]'::jsonb;

-- A retry's report starts with the attempts earlier runs ended for good (`carried`): a lookup whose
-- source answered for another work, a PDF that was another document. Its waterfall skips them.
alter table retrieval_run_report add column carried jsonb not null default '[]'::jsonb;

-- One row per PDF of a bulk upload. A PDF that matched one report is `attached` at once; one that
-- matched none, or more than one, is `held` for the lead with the reports it could be
-- (`candidates`), until the lead assigns it (`assigned`) or discards it (`discarded`).
create table retrieval_upload (
    id uuid primary key,
    project_id uuid not null references project (id),
    document_id uuid not null references document (id),
    file_name text not null,
    licence text,
    uploaded_by uuid not null references actor (id),
    uploaded_at timestamptz not null,
    status text not null check (status in ('attached', 'held', 'assigned', 'discarded')),
    matched_by text check (matched_by in ('file_name', 'content', 'lead')),
    reason text,
    candidates jsonb not null default '[]'::jsonb,
    item_ref text,
    event_id uuid not null
);

create index retrieval_upload_by_project on retrieval_upload (project_id, status, uploaded_at);

-- migrate:down

drop table retrieval_upload;
alter table retrieval_run_report drop column carried;
alter table retrieval_run drop column skipped;
alter table retrieval_run drop column trigger;
alter table enrichment_run drop column retrieve_after;
alter table enrichment_run drop column occurrence_ids;
alter table enrichment_run drop constraint enrichment_run_trigger_check;
alter table enrichment_run add constraint enrichment_run_trigger_check
    check (trigger in ('corpus_built', 'requested'));
