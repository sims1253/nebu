-- migrate:up

-- Screening (WP-09): stages per scope, the items and assignments reviewers screen, their
-- decisions, the AI readings, conflicts, and adjudications. Decisions, undos, and adjudications are
-- append-only; an undo is a new row, never a changed decision.

create table screening_stage (
    id uuid primary key,
    project_id uuid not null references project (id),
    protocol_version_id uuid not null references protocol_version (id),
    protocol_hash char(64) not null,
    topic_id text not null,
    scope_id text not null,
    stage text not null check (stage in ('title_abstract', 'full_text')),
    mode text not null check (mode in ('M1', 'M2', 'M3')),
    reveal text not null check (reveal in ('never', 'on_disagreement', 'always')),
    judge_config_id text not null,
    seed text not null,
    screeners uuid[] not null,
    auditor_id uuid references actor (id),
    opened_event_id uuid not null,
    opened_at timestamptz not null,
    unique (project_id, topic_id, scope_id, stage)
);

-- `kind` and `audit` say which items are sentinels or audit samples. Reviewer reads never select
-- them (see screening_reviewer_queue below).
create table screening_item (
    stage_id uuid not null references screening_stage (id),
    item_ref text not null,
    kind text not null check (kind in ('record', 'sentinel')),
    expected_outcome text check (expected_outcome in ('include', 'exclude')),
    audit boolean not null default false,
    title text not null,
    text text not null,
    facts jsonb not null,
    primary key (stage_id, item_ref),
    check ((kind = 'sentinel') = (expected_outcome is not null)),
    check (not (kind = 'sentinel' and audit))
);

create table screening_assignment (
    id uuid primary key,
    stage_id uuid not null,
    item_ref text not null,
    reviewer_id uuid not null references actor (id),
    role text not null check (role in ('screener', 'auditor')),
    sort_key char(64) not null,
    state text not null default 'pending'
        check (state in ('pending', 'awaiting_final', 'decided')),
    revealed boolean not null default false,
    revision integer not null default 0,
    provisional_decision_id uuid,
    final_decision_id uuid,
    foreign key (stage_id, item_ref) references screening_item (stage_id, item_ref),
    unique (stage_id, item_ref, reviewer_id)
);

create index screening_assignment_reviewer on screening_assignment (reviewer_id, stage_id);

create table screening_decision (
    id uuid primary key,
    assignment_id uuid not null references screening_assignment (id),
    command_id uuid not null unique,
    event_id uuid not null,
    phase text not null check (phase in ('provisional', 'final')),
    outcome text not null
        check (outcome in ('include', 'exclude', 'unclear', 'insufficient_information')),
    reason_criterion_id text,
    criteria jsonb not null,
    evidence_anchor_ids uuid[] not null,
    comment text,
    revision integer not null,
    reveal jsonb,
    shown_at timestamptz not null,
    first_key_at timestamptz,
    decided_at timestamptz not null,
    reveal_shown_at timestamptz,
    recorded_at timestamptz not null,
    check (outcome <> 'exclude' or reason_criterion_id is not null)
);

alter table screening_assignment
    add foreign key (provisional_decision_id) references screening_decision (id),
    add foreign key (final_decision_id) references screening_decision (id);

create table screening_undo (
    id uuid primary key,
    assignment_id uuid not null references screening_assignment (id),
    command_id uuid not null unique,
    event_id uuid not null,
    undone_decision_ids uuid[] not null,
    revision integer not null,
    recorded_at timestamptz not null
);

create table screening_ai_reading (
    stage_id uuid not null,
    item_ref text not null,
    judge_config_id text not null,
    excludes boolean not null,
    rank integer not null,
    criteria jsonb not null,
    flags text[] not null,
    raw_output_hash char(64) not null,
    event_id uuid not null,
    recorded_at timestamptz not null,
    primary key (stage_id, item_ref),
    foreign key (stage_id, item_ref) references screening_item (stage_id, item_ref)
);

-- A decoy is a record the humans and the AI agreed on, mixed into the adjudication queue so that
-- membership does not reveal the AI. `pattern` is the screeners' shared outcome, which decoys
-- match. Adjudicating a decoy changes no outcome.
create table screening_conflict (
    id uuid primary key,
    stage_id uuid not null,
    item_ref text not null,
    kinds text[] not null,
    pattern text check (pattern in ('include', 'exclude')),
    decoy boolean not null default false,
    state text not null check (state in ('open', 'resolved', 'withdrawn')),
    revision integer not null default 0,
    opened_event_id uuid not null,
    opened_at timestamptz not null,
    closed_event_id uuid,
    foreign key (stage_id, item_ref) references screening_item (stage_id, item_ref)
);

create unique index screening_conflict_open on screening_conflict (stage_id, item_ref)
    where state = 'open';

create table screening_adjudication (
    id uuid primary key,
    conflict_id uuid not null references screening_conflict (id),
    adjudicator_id uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    adopted text,
    outcome text not null
        check (outcome in ('include', 'exclude', 'unclear', 'insufficient_information')),
    reason_criterion_id text,
    criteria jsonb not null,
    evidence_anchor_ids uuid[] not null,
    comment text,
    shown_at timestamptz,
    decided_at timestamptz,
    recorded_at timestamptz not null,
    check (outcome <> 'exclude' or reason_criterion_id is not null)
);

create function screening_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception '% rows are append-only (% on %)', tg_table_name, tg_op, old.id;
end;
$$;

create trigger screening_decision_append_only
    before update or delete on screening_decision
    for each row execute function screening_is_append_only();

create trigger screening_undo_append_only
    before update or delete on screening_undo
    for each row execute function screening_is_append_only();

create trigger screening_adjudication_append_only
    before update or delete on screening_adjudication
    for each row execute function screening_is_append_only();

-- What a reviewer may read: their own assignments in one stage, ready once the AI has read the
-- item, in queue order. No sentinel or audit flag, no AI output, nothing of another reviewer. The reveal of the
-- reviewer's own provisional decision comes back only while that decision awaits its final
-- phase. `position` is an ordinal: M3 orders by the AI's rank, M1 and M2 by a seeded key.
create function screening_reviewer_queue(p_stage_id uuid, p_reviewer uuid) returns table (
    assignment_id uuid,
    stage text,
    scope_id text,
    title text,
    text text,
    facts jsonb,
    revision integer,
    state text,
    own_reveal jsonb,
    "position" bigint
)
language sql stable as $$
    select a.id, s.stage, s.scope_id, i.title, i.text, i.facts, a.revision, a.state,
        case when a.state = 'awaiting_final' then d.reveal end,
        row_number() over (
            order by case when s.mode = 'M3' then r.rank else 0 end, a.sort_key, a.id
        )
    from screening_assignment a
    join screening_stage s on s.id = a.stage_id
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    join screening_ai_reading r on r.stage_id = a.stage_id and r.item_ref = a.item_ref
    left join screening_decision d on d.id = a.provisional_decision_id
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
$$;

-- A reviewer's own progress: every assignment counts, ready or not, hidden items included, so
-- the counts cannot single out a sentinel or an audit sample.
create function screening_reviewer_progress(p_stage_id uuid, p_reviewer uuid)
returns table (assigned bigint, decided bigint)
language sql stable as $$
    select count(*), count(*) filter (where a.state = 'decided')
    from screening_assignment a
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
$$;

-- migrate:down

drop function screening_reviewer_progress;
drop function screening_reviewer_queue;
drop trigger screening_adjudication_append_only on screening_adjudication;
drop trigger screening_undo_append_only on screening_undo;
drop trigger screening_decision_append_only on screening_decision;
drop function screening_is_append_only;
drop table screening_adjudication;
drop index screening_conflict_open;
drop table screening_conflict;
drop table screening_ai_reading;
drop table screening_undo;
alter table screening_assignment
    drop constraint screening_assignment_provisional_decision_id_fkey,
    drop constraint screening_assignment_final_decision_id_fkey;
drop table screening_decision;
drop table screening_assignment;
drop table screening_item;
drop table screening_stage;
