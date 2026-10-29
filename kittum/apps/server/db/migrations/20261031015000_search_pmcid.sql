-- migrate:up

-- Europe PMC and PubMed name a record's PubMed Central ID; deduplication matches on it.
alter table source_occurrence add column pmcid text;
create index source_occurrence_pmcid on source_occurrence (project_id, pmcid) where pmcid is not null;

-- migrate:down

drop index source_occurrence_pmcid;
alter table source_occurrence drop column pmcid;
