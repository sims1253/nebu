-- migrate:up

-- A duplicate on a report identifier alone (DOI, PMID, PMCID), which the lead may confirm with all
-- the others in one step.
alter table proposal add column exact boolean not null default false;

-- migrate:down

alter table proposal drop column exact;
