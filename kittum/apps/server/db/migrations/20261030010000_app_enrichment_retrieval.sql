-- migrate:up

-- Enrichment runs over a corpus revision, and open-access retrieval runs over the reports sought
-- for retrieval. Each run is one job; these rows hold what it did, record by record and report by
-- report, so a retried job resumes where it stopped and the workbench shows its progress. The
-- enriched fields themselves are `record_enrichment` rows; the files are `acquisition` rows.

create table enrichment_run (
    id uuid primary key,
    project_id uuid not null references project (id),
    identity_revision_id uuid not null,
    trigger text not null check (trigger in ('corpus_built', 'requested')),
    requested_by uuid not null references actor (id),
    requested_at timestamptz not null,
    records integer not null,
    job_id uuid not null references job (id),
    event_id uuid not null,
    finished_at timestamptz
);

create index enrichment_run_by_project on enrichment_run (project_id, requested_at);

-- One row per source occurrence the run enriched. `asked` and `answered` list sources; `failed`
-- holds `{source, reason}` for each source that failed without answering for this record.
create table enrichment_run_record (
    run_id uuid not null references enrichment_run (id),
    occurrence_id uuid not null,
    asked jsonb not null,
    answered jsonb not null,
    failed jsonb not null,
    retracted boolean not null,
    event_id uuid not null,
    primary key (run_id, occurrence_id)
);

create table retrieval_run (
    id uuid primary key,
    project_id uuid not null references project (id),
    requested_by uuid not null references actor (id),
    requested_at timestamptz not null,
    job_id uuid not null references job (id),
    event_id uuid not null,
    finished_at timestamptz
);

create index retrieval_run_by_project on retrieval_run (project_id, requested_at);

-- The reports a run walks the waterfall for, fixed when it is requested. Each report's state is
-- resumable: `locations` (`documents.FullTextLocation`s) are fixed when the job first takes the
-- report, every attempt is appended to `attempts` in a transaction of its own with its ledger
-- event, `acquisition_id` is set by the attempt that got a PDF, and `outcome` once the report is
-- finished (after the PDF is attached, in another transaction).
create table retrieval_run_report (
    run_id uuid not null references retrieval_run (id),
    report_id uuid not null,
    ordinal integer not null,
    outcome text check (outcome in ('acquired', 'not_acquired', 'no_location', 'had_full_text')),
    locations jsonb,
    attempts jsonb not null default '[]'::jsonb,
    acquisition_id uuid references acquisition (id),
    event_id uuid,
    finished_at timestamptz,
    primary key (run_id, report_id)
);

-- migrate:down

drop table retrieval_run_report;
drop table retrieval_run;
drop table enrichment_run_record;
drop table enrichment_run;
