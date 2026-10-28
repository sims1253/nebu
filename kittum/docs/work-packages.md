# Work packages

Each package is one module or one cross-cutting piece, sized for one agent and one branch. Every
interface exists as a stub, and the shared fakes exist, so all packages can start now. "Needs"
lists what a package calls at integration time, not what it waits for.

The two-week spike in [DESIGN.md](../DESIGN.md#two-week-spike) picks from these in order: WP-01,
02, 03, 05, 08, 13, then 09, 10, 11, 14.

## Status

| WP | Module | Status |
| --- | --- | --- |
| 00 | Scaffold: `core`, layout, CI, compose, contracts pipeline | Done |
| 01 | `ledger` | Done: Postgres and SQLite stores, checkpoints, `seal`, `kittum verify`; TSA and WORM copies open |
| 02 | `runs` | Done: job queue, worker, receipts, decider helper, fakes; run manifests and `diff` open |
| 03 | `protocol`, `profiles` | Done: schema, profiles, compile, diff, rendering, persistence, routes |
| 04 | `gateway` | Done: httpx transport, policies, shared buckets, budgets, cache, attempts; AppContext wiring open |
| 05 | `search` | Done |
| 06 | `enrich` | Clients, stored enrichment, and full-text waterfall done; local tagging open |
| 07 | `corpus` | Dedupe, link proposals, immutable identity revisions, PRISMA counts done |
| 08 | `judge` and `models` | Done; ledger store and job wiring wait for WP-01 and WP-02 |
| 09 | `screening` | Done: M1 to M3, blinding, reveal, sentinels, audits, adjudication, judge jobs, metrics, routes; open items listed |
| 10 | `documents` | Parsers, layers, anchors, re-anchoring, viewer routes, and acquisition done; source resolvers for the waterfall open |
| 11 | `extraction` | Types done; open |
| 12 | `appraisal` | Types done; open |
| 13 | `stats` | Done: intervals, stopping, capture-recapture, conformal threshold, agreement |
| 14 | `report` | Done: PRISMA flow, RIS, search report, AI disclosure, bundle exporters and `seal_bundle`, `gba_amnog` targets, routes; open items listed |
| 15 | `api`, `app` | Done: composition root, project lifecycle (service, routes, CLI), `serve` with the workbench, `worker`, `demo seed`; authentication open |
| 16 | Workbench | Done: wired to the API with a reviewer picker; screening, full text, adjudication, progress, protocol, search, studies, report rendering and sealing; end-to-end journey in CI. Extraction review on the server waits on WP-11's screen wiring |
| 17 | Evaluation harness | Open |

## WP-01 Ledger

Module `kittum.ledger`. Needs: `core`.

- A Postgres store on the foundation tables: append with per-project serialization (advisory lock
  or `select ... for update` on the chain head), events, and verify.
- A SQLite store with the same behaviour, for the handover bundle and tests.
- Signed checkpoints (Ed25519 with `cryptography`), with the public key ID recorded.
- `seal(cutoff_seq)`: the bundle manifest and the offline `verify` command.
- Acceptance: both stores pass one shared test suite. Verify catches a changed payload, a removed
  event, a reordered event, and a cut tail after a checkpoint.

**Done.** `PostgresLedger`, `SqliteLedger`, and `InMemoryLedger` share one implementation of the
chain rules (`ledger/store.py`); `tests/ledger/test_stores.py` runs one suite over all three.

- Postgres appends take `pg_advisory_xact_lock` on the project, held until the caller commits.
- `checkpoint(tx, project_id, signer)` verifies the chain, signs the head with Ed25519, and stores
  the row. `verify(tx, project_id, checkpoint=..., key=...)` catches a cut tail and a chain
  rewritten from some point on, which the chain alone cannot. `VerifyReport.problem` names what
  failed. The key comes from `KITTUM_CHECKPOINT_KEY` (`Settings.checkpoint_key_path`); the key ID
  is the SHA-256 of the raw public key.
- `seal(tx, project_id, cutoff_seq, store, out_dir, signer=..., actor_id=...)` writes
  `kittum.sqlite` (ledger through the cutoff, checkpoints, artifact index), `artifacts/`, and a
  signed `manifest.json`, and records a `ledger.bundle_sealed` event. Modules add tables and
  artifacts with `register_exporter(exporter)`; a withheld artifact is indexed by hash with a
  reason, and a withhold wins over an include.
- `kittum verify <bundle> [--public-key pem]` checks offline: the manifest signature, the
  checkpoint signature, chain continuity to the cutoff, file hashes, unlisted files, and the
  artifact index. Without `--public-key` it uses the bundle's own key and says the key is not
  pinned.

Left open:

- RFC 3161 timestamps over checkpoints (`tsa_token_hash` stays empty) and copies of checkpoints
  outside the database (WORM storage). Until then, pass `verify` a checkpoint kept elsewhere.
- Scheduled checkpoints at stage gates and daily, once `runs` and the stage gates exist.
- The `checkpoint` table has no append-only trigger, because it is in the foundation migration.
  A trusted external copy covers this; a trigger would be a small foundation change.
- Bundle contents from DESIGN.md beyond the ledger: `dossier/`, `registry/`, count
  reconciliation, and the "every accepted value has an anchor" check belong to the exporters of
  `report`, `extraction`, and `documents`.

## WP-02 Runs and jobs

Module `kittum.runs`. Needs: `ledger`.

- `JobQueue` on the `job` table with `for update skip locked`, leases, heartbeats, retries with
  backoff, and dead-lettering. Unique `stage_key` prevents double publication.
- Command receipts: a retried command returns the stored receipt and writes nothing.
- The decider pattern: a helper that commits events, rows, receipt, and jobs in one transaction.
- A worker loop with graceful shutdown, and an in-memory queue fake.
- Acceptance: two workers never run the same job; a crashed worker's job is reclaimed after its
  lease; a replayed command is a no-op.

**Done.** `tests/runs/test_queue.py` runs one suite over `PostgresJobQueue` and the
`InMemoryJobQueue` fake.

- `JobQueue` methods take the caller's `tx`. A claimed `Job` is the lease token: `heartbeat`,
  `complete`, and `fail` check owner and attempt and raise `LeaseLost` otherwise. Expired leases
  are reclaimed; an expired lease on the last attempt dead-letters the job.
- `fail` requeues with exponential backoff and jitter (`RetryPolicy`), and marks the job `dead`
  after `max_attempts`; `retryable=False` (or `PermanentJobError` in a handler) marks it `failed`.
- `enqueue` with an existing `stage_key` returns the existing job, or raises `StageKeyConflict` if
  the inputs differ.
- `Commands(ledger, queue, receipts).execute(tx, command, decide, load=..., apply=...)` locks the
  command ID, returns a stored receipt unchanged, or decides, appends events, applies rows,
  enqueues jobs, and saves the receipt in one transaction.
- `Worker(db, queue, handlers, name=...)` claims, heartbeats in a thread, and completes the job in
  the same transaction as the handler's `JobOutput.publish`. `stop()` (or `stop_on_signals()`)
  lets the current job finish. Fakes: `InMemoryJobQueue`, `InMemoryReceipts`, `NullTransactions`.

Left open:

- Run manifests, cache keys, branch runs, and `runs.diff` (DESIGN.md "Runs and reproducibility").
- A `kittum worker` entry point that builds handlers from the modules.
- Receipts do not store a command hash, so a reused command ID with a different payload but the
  same type returns the old receipt. Detecting that needs a column on `command_receipt`.

## WP-03 Protocol

Module `kittum.protocol` (domain-neutral core) and `kittum.profiles` (review-community profiles).
Needs: `core`, `db`, `ledger`.

Done:

- A generic schema: review questions (each a scope) with PICO, comparators (optionally
  alternatives), and outcome IDs; outcomes with a category; effect modifiers; search topics with
  criteria, source plan, reference set, screening mode with audit and stopping plans, and a
  freshness policy. All human text is localized (`{en: ..., de: ...}`) with a required
  `default_language`.
- Profiles: `register_profile(Profile)` adds extension schemas (protocol, question, comparator,
  outcome, topic, criterion), vocabularies (`outcome_category`, `topic_kind`), rules, labels, and
  default effect modifiers. The core never imports a profile; `api.create_app` calls
  `kittum.profiles.load_profiles()`.
- `kittum.profiles.gba_amnog`: dossier and Anwendungsgebiete, Teilpopulationen with zVT
  (comparators as alternatives, "nach ärztlicher Maßgabe", off-label options), JCA PICOs as linked
  questions, the four AMNOG endpoint categories, Modul 4 search kinds, the three-month search age,
  the Modul 4 source minimum, German as the default language.
- `compile(source)` from YAML, JSON, or a mapping: every issue with its path, duplicate YAML keys
  with line numbers, cross-checks (unique IDs, scopes exist, every topic and scope decidable at
  title and abstract, mode rules, reference identifiers), and a content hash over RFC 8785 JSON.
- `diff(a, b)`: added, removed, and changed criteria and scopes, mode changes, and field-level
  changes keyed by ID.
- `render_question` (reviewer question with answer keys, profile slot labels) and
  `render_prompt` (judge fragment asking for a verbatim quote, languages in stable order).
- Persistence in `protocol_version` (migration `20261028140000_protocol_versions.sql`):
  `save_draft` (idempotent per content hash), `publish` (human approver, `protocol.published`
  ledger event, reason required for a mode change, then frozen by trigger), `get_version`,
  `list_versions`, `latest_published`. `testing.InMemoryProtocolStore` is the fake.
- Routes: `POST /api/protocol/validate`, `POST /api/projects/{id}/protocol-versions`,
  `POST .../{version_id}/publish`, `GET .../protocol-versions`, `GET .../{version_id}`.
- Examples: `examples/protocols/capivasertib.yaml` (G-BA D-1110, `gba_amnog`) and
  `examples/protocols/nicotine-replacement.yaml` (no profile).

Left:

- Swap the test-only `SqlLedger` (tests/protocol/support.py) for the WP-01 Postgres ledger, and
  give `AppContext.ledger` a real default once it exists.
- Approver identity comes from the request body until the API has authentication (WP-15).
- A profile for EU JCA dossiers as a report target, if JCA needs more than linked questions.

## WP-04 Gateway

Module `kittum.gateway`. Needs: `core`.

- Done: `HttpxTransport` on httpx2, with timeouts, redirects, and a `User-Agent` naming the
  Kittum version and the configured contact email.
- Done: default policies for PubMed, ClinicalTrials.gov, Europe PMC, OpenAlex, Crossref,
  Unpaywall, and PubTator, 10% under docs/sources.md (`default_policies()`); a caller overrides
  any of them by passing its own mapping. A contact email moves Crossref to the polite pool and
  an NCBI key raises PubMed's rate.
- Done: `SourceGateway` with a pyrate-limiter bucket per source. The Postgres bucket shares the
  limit across processes; `buckets="memory"` is for tests. One table per source, named
  `ratelimit___gateway_<source>`. A per-source concurrency cap is enforced with Postgres advisory
  slots held for each send, so it spans workers too.
- Done: daily budgets in `gateway_budget`, consumed per attempt, so a new gateway instance
  continues where the previous one stopped. Exhaustion raises `SourceBudgetExhausted`; the
  caller decides whether to wait for the next day or fail the job.
- Done: `Retry-After` (seconds or HTTP date) and `X-RateLimit-Remaining`/`X-RateLimit-Reset`
  pause a source before the next attempt, with the full deadline in `gateway_cooldown`, so every
  worker waits. Retries with stamina on 429, 502, 503, and 504, five attempts, exponential
  backoff with full jitter.
- Done: content-addressed cache: the body goes to `ArtifactStore`, `gateway_cache` maps the
  canonical request hash to status, headers, and body hash. `use_cache=False` skips the read but
  stores the fresh response for later replays. Every transport attempt lands in `gateway_attempt`.
- Done: `FakeTransport` scripts a sequence of responses (`kittum.gateway.testing`).
- Left: entry points do not build a gateway yet. `AppContext` needs a `gateway` field (an
  interface change outside this module) before search and enrichment can call it.
- Left: PubMed's and OpenAlex's API keys are not attached to requests here; adapters pass them
  as request parameters. Retry-After on 503 is honoured, but there is no circuit breaker or
  source-health memory beyond the cooldown deadlines.
- Acceptance met: tests against real Postgres with `FakeTransport` show a 429 then 200 with both
  attempts recorded, five failures with a clear error, a cache hit with no transport call,
  independent buckets for two sources, a budget that survives a new gateway, the concurrency cap,
  and cooldowns shared across gateway instances.

## WP-05 Search

Module `kittum.search`. Needs: `gateway`, `protocol`, `ledger`.

- PubMed E-utilities: ESearch with history, date windows under the 10,000 cap, EFetch, query
  translation capture, structured abstracts with labels kept, registry IDs from `DataBankList` and abstract.
- ClinicalTrials.gov API v2 with Essie syntax, pagination across multiple pages, mapping to NCT ID, status, and phase.
- File imports: RIS (Generic, Cochrane Library CENTRAL, Embase) and CSV (CTIS, EU-CTR).
- Search execution and source occurrence persistence in Postgres with audit ledger event (`search.executed`).
- HTTP API routes (`/api/projects/{project_id}/search/*`) registered and contract generated.
- `FakeSource` in `search/testing.py`.
- Acceptance: recorded real responses in `tests/search/fixtures/` reproduce counts, query translation, and PRISMA-S fields; tests pass end-to-end without network.
- `run` and `import_file` take the caller's ledger, actor, and artifact store. Occurrence IDs are
  assigned at persistence; every raw hash must be in the store before rows are written; the
  `search.executed` payload is built once (`prisma_s_payload`). Routes take `actor_id` in the body
  and answer 503 without a ledger or gateway.
- PubMed date windows span 1000/01/01 to 3000/12/31 and must cover the original count; PMIDs are
  deduplicated across windows, and unique articles plus skipped book records must equal ESearch's
  `Count`, or the run fails. CSV imports parse strictly and reject rows wider or narrower than the
  header.
- Left: take the actor from the session once auth exists; run live searches as a job (the HTTP calls
  currently happen inside the route's transaction); map `PubmedBookArticle` records (today they are
  skipped and listed in `skipped_record_ids`).

## WP-06 Enrichment

Module `kittum.enrich`. Needs: `gateway`, `search`, `models`.

- Done: OpenAlex (works by DOI and PMID in OR batches, inverted-index abstracts, OA locations),
  Crossref (`update-to`, `updated-by`, and `relation` notices, `clinical-trial-number`, polite
  pool), Europe PMC (batched ID search, `synonym=FALSE`, full-text availability), Unpaywall (best
  OA location and licence), and PubTator 3 (BioC JSON entities re-anchored in the title plus
  abstract).
- Done: "first healthy source" per field. A source that errors or is out of budget is recorded in
  `Enrichment.failures` and the next one answers; `FieldSource` records which source supplied each
  field. Identifier lookups batch where the API allows.
- Done: `enrich(tx, project_id, records, gateway)` writes one `record_enrichment` row per
  occurrence, replacing an earlier row for the same occurrence. `EnrichmentConfig` carries the
  contact email and OpenAlex key that entry points map from `Settings`.
- Done: `full_text_candidates` lists Europe PMC XML and PDF, then Unpaywall, then OpenAlex file
  URLs, each with its licence.
- Done: recorded real responses in `tests/enrich/fixtures/`, including a retracted paper whose
  Crossref notice DOI is flagged.
- Open: local entity tagging with the OpenMed GLiNER models, for text that must stay local.

## WP-07 Corpus

Module `kittum.corpus`. Needs: `search`, `enrich`, `ledger`.

- Dedupe rules over `SourceRecord`s: report identifiers (exact DOI, PMID) and rapidfuzz normalized title plus year and first author surname, keeping every occurrence.
- Trial identifiers (NCT, EudraCT, CTIS numbers) drive only `same_study` proposals and never merge records or reports; distinct publications of one trial with the same NCT number stay separate reports linked to one study.
- Link proposals for report (`same_record`, `same_report`) and study (`same_study`) with evidence strings.
- Immutable identity revisions in Postgres (`report`, `study`, `corpus_revision`, `identity_revision`, `identity_link`, `proposal` tables with migration `20261028020000_corpus_identities.sql`). A revision is written with its links in one transaction and then finalized; triggers reject any later update, delete, or added link.
- Merge and confirm move whole groups: `same_record`/`same_report` union complete report groups (and their studies), `same_study` unions complete study groups. A stronger confirmed identity upgrades the chosen occurrences' labels and never downgrades one. A split names its level: `report` moves occurrences into new reports of the same study, `study` moves complete report groups into new studies.
- A record without identifiers never bridges two different DOIs or PMIDs into one proposal; titles conflict, and are never fuzzy-matched, only when both name a trial (an acronym with two capitals, or a word both titles put before a number, other than common words such as stage or cohort), phase, follow-up length, or study number and the values differ (KEYNOTE 024 vs 042, 5-year vs 3-year); a number on one side only, such as a citation's year or pages, never blocks a match.
- Ledger integration: human confirmation, split, and merge write audit events (`corpus.confirmed`, `corpus.merged`, `corpus.split`) via `Ledger.append(tx, EventDraft)` to the hash-chained ledger.
- PRISMA 2020 identification counts from a frozen identity revision: records identified per source, duplicates removed, unique records, studies identified.
- HTTP routes in `src/kittum/api/routes/corpus.py`: list proposals, confirm, merge, split, get counts, all scoped to the project (404 for another project's revision, 400 for invalid merges and splits).
- `FakeCorpus` in `src/kittum/corpus/testing.py` for downstream modules.
- End-to-end tests against Postgres verifying PRISMA counts, revision immutability, history preservation on split, and separate reports for same-NCT publications.

## WP-08 Judge and models

Modules `kittum.judge` and `kittum.models`. Needs: `protocol`, `core`.

- An OpenAI-compatible backend for vLLM and Ollama with JSON Schema-constrained output; the model
  recipe registry with the licence allowlist; the confidentiality check.
- `judge`: the prompt per criterion set, decoding, `find_quote` verification, keyword and negation
  checks, downgrade to `unclear`, raw output capture.
- Gold-data metrics: per-judge recall and specificity, the double-fault rate, run-to-run kappa.
- Acceptance: recorded completions cover verified, unverified, paraphrased, and negated quotes.

**Done.**

- `models.OpenAICompatibleBackend`: `response_format` with `json_schema`, temperature 0, a seed,
  the recipe's `extra_body` (thinking switches), raw request and response bytes in the artifact
  store, the model identity the server reports, thinking removed before the JSON (`<think>`,
  Gemma channels, harmony, code fences, or the `reasoning` field), retries on transport errors
  only. `backends_from_endpoints` builds backends from `Settings.llm_endpoints`. A `local_only`
  request to a `hosted` recipe raises `ConfidentialityError` before anything is sent.
- `models.ModelRegistry` loads `models/recipes.yaml`: Qwen3.8-27B (thinking off and on),
  Gemma-4-31B-it, Nemotron 3.5 Super VL NVFP4, and Gemma 4 E4B, Gemma 4 12B, and Qwen3.5-4B on
  Ollama. Revisions and weight hashes were checked against Hugging Face and the Ollama registry on
  28.10.2026. Loading rejects licences outside `ALLOWED_WEIGHTS_LICENCES`.
- `judge.judge` and `judge.judge_many`: one call per record and scope, a per-criterion schema
  generated from Pydantic, verification and downgrades with `failed_check`, retry with the next
  seed on malformed output, then `unclear` with the `invalid_output` flag. Records that try to
  instruct the model cannot be excluded. A cheap, conservative filter (`judge/verify.py`)
  downgrades `not_met` answers whose evidence is not structurally supported: one sentence, a
  whole-word key term without a negating compound prefix, an English or German quote, at most one
  negation cue and it scoped to the key term, and the right polarity for an include or exclude
  criterion. `Verdict.excludes` is a proposal for humans and adjudication, never a final
  exclusion. `judge_many` stores verdicts (migration
  `20261028120000_judge_verdicts.sql`), artifacts, and `judge.verdict_recorded` events, and
  skips records already judged under the same cache key.
- `judge.judge_metrics`, `pair_metrics` (double-fault rate, OR rule, Cohen's kappa), and
  `fleiss_kappa` for run-to-run agreement.

**Left.**

- `judge_many` holds the caller's transaction across model calls. When WP-02 lands, split it into
  a job that calls the model and a command that records the verdict.
- The judge is built against WP-03's `Criterion` (`text` per language, `polarity`, `keywords`)
  and renders criteria with `kittum.protocol.render_prompt`. This branch carries a temporary copy
  of those types in `protocol/__init__.py`; drop it when the protocol branch is merged.
- Ollama reports only the tag as the model identity. Record the blob digest from `/api/show` if
  audits need more.
- Trade-off, accepted for recall: a quote that contradicts a criterion by value without naming
  one of its terms ("patients aged 12 to 17 years" against "adults") is `unclear`, as are
  exclusions on mixed conditions ("adults without EGFR mutation") and negations more than three
  tokens or a clause away from the key term. Protocol `keywords` let authors anchor such criteria
  ("aged", "adolescent"), but an affirmative inclusion condition still needs a scoped negation.
  Measure the work saved this costs on gold data before loosening any rule.
- Accepted limitations of the filter, not to be chased with more patterns: foreign content words
  that carry negation ("Melanoma assente"); a negation in an intervening clause about another
  condition within the window ("Patients with no diabetes received chemotherapy"); evidence about
  another arm or population; partial matches ("skeletal metastases" against "brain metastases");
  and in general anything semantic, since the filter is not a semantic verifier. Mitigation: every
  record is seen by a human, an AI exclusion is a proposal, and disagreements, AI-only exclusions,
  and audits go to adjudication. Requiring all wording terms (up to three) in the quote would
  narrow partial matches, but it rejects the positive controls ("Patients without prior
  chemotherapy" against "Previously treated with chemotherapy"), so it is not done.
- Polarity is read in English and German only (`judge.verify.POLARITY_LANGUAGES`). A not_met
  on a criterion without an en or de wording, or with a quote that is not positively English or
  German, becomes `unclear` (`language_unverifiable`). Add a language by adding its function
  words and negation cues.
- A live test against Ollama or vLLM; the tests use recorded response bodies.

## WP-09 Screening

Module `kittum.screening`. Needs: `protocol`, `corpus`, `judge`, `ledger`, `runs`, `stats`.

- Assignments per mode (M1 to M3), blinding enforced in queries, reveal-after-commit with
  provisional and final decisions, sentinels and audit samples hidden from reviewers, conflicts,
  adjudication.
- Acceptance: a reviewer can never read the other reviewer's decision or the AI verdict before
  committing; tests prove it at the service boundary.

**Done.** `Screening` (migration `20261029010000_screening_tables.sql`); every write is a
`Commands.execute` command with a pure decider, a receipt, and ledger events.

- `open_stage(spec)` opens one scope at one stage of a published protocol version. The topic's
  mode fixes the screeners (two in M1 and M3, one in M2). Every stage names an auditor, who is not
  a screener, and its adjudicators. `sentinel_percent` of the record count (rounded up) is drawn
  from the given sentinels and given to every reviewer; `random_audit_percent` of the records also
  go to the auditor. Both draws are seeded and named in `screening.stage_opened`. M1 and M2 stages
  need a nonzero audit percentage (see the extra screens below). Criteria that apply to the
  whole topic apply to each of its scopes (`criteria_for`).
- `request_judging` enqueues `screening.judge` jobs in batches (stage keys per stage, judge
  config, and batch, so asking again adds nothing); `judge_handler(db, screening, runner)` runs
  them in a `Worker`. The runner is `functools.partial(judge.judge_many, ...)`, built by the entry
  point because screening may not import `models` or `storage`; verdicts become the stage's AI
  readings through a command whose ID derives from the job (`screening.ai_read`). An item enters
  queues once the AI has read it.
- Queues: M1 and M2 follow a seeded order per reviewer; M3 follows the AI's order (proposed
  exclusions last, then by criteria not met), shared by both screeners. `queue` returns batches
  after a cursor (`after`), as the workbench prefetches five.
- Two readings of each verdict. `ai_excludes` is `Verdict.excludes` (verified by the filter); it
  counts where an AI exclusion would remove or deprioritize a record: the M2 disagreement when the
  human includes, the M3 order, and the audit strata. The model's own reading includes a record
  when no criterion's model-reported status (`downgraded_from` if set, else `status`) is
  `not_met`; it counts where the AI argues for keeping a record that humans excluded: the M1
  AI-include rule and the M2 disagreement when the human excludes. A `not_met` that only the
  filter downgraded is therefore agreement with a human exclusion, and no exclusion the filter
  could not verify is ever revealed against an inclusion. A record that tried to instruct the
  model has no trustworthy own reading and counts as included. (Integrator's decision, after the
  first review: the filter stops wrongful exclusions; it must not send nearly every record both
  humans exclude to adjudication.)
- `decide`: a provisional decision, then a reveal if the stage's `reveal` setting and the AI say
  so (on disagreement as above: the AI's verified reason to exclude, or its reading of the human's
  criterion), then a final decision. Both are stored with the client's timing (shown, first key,
  decided, reveal shown). Clients see an opaque `etag` per item and conflict, random per state,
  never a revision count; they send it back as `expected_etag` (`StaleRevision` if it is old).
  Revisions stay internal and in the ledger. A replayed command returns the stored result.
  Exclusions need a criterion ID. `undo` returns the item to the queue under a new token, keeps
  the old decisions, and withdraws an open conflict; an adjudicated item cannot be undone.
- AI-only disagreements never go to adjudication; they become blind extra screens (integrator's
  decision after the third review, replacing an earlier design with decoys in the adjudication
  queue, which could not be made tight). When M1's screeners both exclude while the model saw no
  failing criterion, or M2's lone human and the AI disagree (provisionally or finally, the
  protocol's census), the record goes to the stage's auditor: a new auditor assignment, seeded into
  the auditor's queue among the random audit samples and sentinels and alike in every field, or
  the auditor's existing one if the record is an audit sample. The ledger records each routing as
  `screening.ai_disagreement_rescreen` (item, reason, auditor) for the AI disclosure. The auditor
  screens it like any record, with the normal reveal rules for their own decision. If they agree
  with the screeners, the human decision stands; if they disagree, that human disagreement goes to
  adjudication.
- Conflicts are human disagreements only: the screeners disagree on inclusion (or at full text
  exclude on different criteria), or the auditor's screen disagrees with them. Membership in the
  adjudication queue therefore says nothing about the AI, and the AI column that opens after the
  adjudicator commits leaks nothing about other items. `adjudication_queue` shows each human's
  final decision under a pseudonym (A, B, C), never to someone who reviewed the record, in an order
  hashed from random conflict IDs; only the stage's adjudicators may adjudicate. `adjudicate`
  adopts a column or takes an own decision. `outcomes` gives each record's current outcome
  (include, exclude, conflict, or not yet screened, which includes waiting for an extra screen)
  for the lead and later stages.
- `status`: screeners and the auditor see only their own counts; adjudicators the screening
  progress and their pending adjudications; anyone else (the methods lead) the stage: conflicts,
  the extra screens and their outcomes (`rescreens`: triggered, pending, upheld, adjudicated),
  and the metrics below.
- Blinding below the service: reviewer reads go through the SQL functions
  `screening_reviewer_queue` and `screening_reviewer_progress`, which return only the reviewer's
  own assignments in one stage, no AI output (except their own reveal while it awaits the final
  decision), no sentinel or audit flags. Two topics that screen the same scope keep separate
  queues and counts; a scope in several topics needs `topic_id`. The lead's view has kappa and
  PABAK (human pairs; human-AI against the
  model's own reading as `human_ai` and against the verified reading as `human_ai_verified`; all
  on blind provisional decisions), reveals shown and followed, the audit gauge (sentinel accuracy,
  `stats.elusion_estimate` over the records excluded without adjudication, in two strata by
  `ai_excludes`), and the M3 stop test (`stats.stop_test` over the fully screened
  prefix of the AI order, with the minimum screened share). An open conflict in the tested prefix
  counts as relevant in the p-value but withholds certification: the verdict is
  `pending_conflicts`, with their number, until they are adjudicated.
- Full text: per-criterion readings (met, not met, unclear) with evidence anchor IDs (opaque UUIDs
  until WP-10); include, exclude, or insufficient information. Every outcome cites at least one
  evidence anchor, and an exclusion cites a not-met criterion with evidence.
- Routes in `api/routes/screening.py`: `GET .../screening/queue`, `POST .../assignments/{id}/decide`,
  `POST .../assignments/{id}/undo`, `GET .../adjudications`, `POST .../adjudications/{id}/decide`,
  `GET .../status`. The caller's actor ID comes from the `X-Kittum-Reviewer` header until WP-15
  adds authentication; it is a stand-in, not a security boundary.
- `tests/screening/test_rescreen.py` covers the extra screens in M1 and M2, upheld and disputed;
  `test_escalation.py` the two readings (a downgraded `not_met` with both M1 humans excluding is
  not escalated and stays in the audit frame; a model that saw every criterion met gets an extra
  screen; the same in M2); `test_topics.py` and
  `test_m3_stop_waits_for_conflicts_in_the_tested_prefix` the first review's findings.
- Tests (`tests/screening/`) run the nicotine replacement example, the recorded PubMed search and
  a CENTRAL export, the corpus, and the judge on `RecordedBackend`, against Postgres, through the
  worker and the API.

Left open:

- The protocol has no `reveal` field; it is set per stage when the stage opens and recorded in the
  ledger. If it belongs in the protocol (WP-03), read it from there.
- Screening records come in as `ScreeningRecord`s: `corpus` exposes no read of an identity
  revision's reports, and screening may not import `search`. An entry point (CLI or route) that
  builds them from the corpus, and one that opens stages and runs a judge worker, are left to
  WP-15 and the `kittum worker` entry point of WP-02.
- `judge_many` holds the job's transaction across model calls (WP-08's open item). The handler
  records verdicts in a transaction of its own, so a rerun calls no model, but long batches hold a
  connection.
- The audit sample is drawn up front from every record, so the share of the excluded population
  it covers is close to, not exactly, `random_audit_percent`. A found miss goes to adjudication;
  the queue of the 20 nearest AI-excluded records (DESIGN.md) is not built.
- M3 reports the stop test but does not stop the queues; stopping is the methods lead's decision.
- One judge per stage. A second model family as an independent judge, and capture-recapture over
  the provisional decisions, are not wired.
- Changing the mode or the reveal setting of an open stage, and new scopes on a screened corpus,
  need a new stage; screening does not carry decisions across protocol versions yet.
- Reviewer metrics for themselves after a session, the time-to-first-key summaries, the
  disagreement rate per criterion, and break suggestions after a missed sentinel are not computed;
  the timings they need are stored.
- The example protocols' M1 topics sample no records for audit, so their stages are refused
  until the protocol sets `random_audit_percent` (the tests set 10%). The examples belong to
  WP-03.
- The auditor's queue grows when an extra screen arrives, and the share of extra screens among
  the audit samples sets how much an item there hints that the AI disagreed. A larger audit
  percentage dilutes it; the rate is not tuned against the expected number of disagreements.
- The judge's filter confirms few exclusions on the example protocols (wordings with "without", or
  keywords without a negation, fail the polarity check), so the verified reading mostly includes.
  Escalation uses the model's own reading, so this no longer floods M1 adjudication, but M3 order
  and the audit strata see few verified exclusions.

## WP-10 Documents

Module `kittum.documents`. Needs: `gateway`, `ledger`.

- Page triage with pdf-inspector; parsers for PyMuPDF, pypdfium2, and Docling behind `Parser`;
  OCRmyPDF for scans; NFC canonical text with word boxes in PDF points.
- Layer storage by content hash; `resolve` with exact, candidate, ambiguous, and unresolved.
- The full-text waterfall (docs/sources.md).
- Acceptance: fixture PDFs with multi-column text and a merged-header table; anchors survive a
  re-parse with a different engine.

Done:

- `triage`: pdf-inspector's page classification and garbled-text flag, plus a PyMuPDF count of
  characters a working ToUnicode map never yields. Each page is `text`, `ocr`, or
  `broken_encoding`, and the layer keeps the verdict.
- `PyMuPDFParser` (the base layer), `PdfiumParser` (differential checks, `compare_layers`), and
  `DoclingParser` (the `docling` extra: Heron layout order and TableFormer grids, every character
  from PyMuPDF). All share one assembler: NFC text, ligatures expanded, line-end soft hyphens kept as
  "-" so `find_quote` joins them, multi-column reading order, running headers first, tables as
  rows of cells. Boxes are unrotated top-left PDF points; the reading frame follows the text
  direction, so sideways landscape pages and pages turned by /Rotate both read in order.
- Tables without PyMuPDF's Layout model (`find_tables(use_layout=False)`, per
  docs/document-tools.md): ruled tables from `find_tables`, three-rule tables from their "Table N"
  caption and full-width rules. The grid comes from the words: rows are printed lines, columns are
  gaps most rows leave empty, header phrases crossing a gap or underlined by a rule span columns,
  and empty header cells merge upwards. Cells carry spans and header paths.
- `OcrMyPdfParser` OCRs the pages triage flags. It needs `ocrmypdf` and `tesseract`; without them
  `ocr_available()` is False and the parser refuses to start. Not exercised here (not installed).
- Storage (migration `20261028025000_documents_layers.sql`): `document` by SHA-256, `acquisition`,
  immutable `layer` (unique per document and recipe hash, JSON in the artifact store, output hash
  = artifact hash) and `anchor`, `anchor_mapping`, and `page_render`. `derive(tx, document, parser,
  artifacts=...)` is idempotent per recipe. `ingest` refuses truncated PDFs.
- `make_anchor(layer, span)`: quote with 32 characters of context, one box per printed line (partial
  words cut along their reading direction, so sideways words too), and cell refs.
  `resolve(anchor, layer)` gives `exact` (same place, unchanged context), `candidate` (unique match
  elsewhere, changed context, or a fuzzy respelling whose numbers are unchanged), `ambiguous` (with
  every match as an alternative), or `unresolved`; repeated values resolve by region. Matches must
  start and end on token boundaries, so "12" never matches inside "123" or "12.5". `resolve` and
  `remap` raise `DifferentDocument` for a layer of another document: a revised PDF is a different
  document and needs a document-revision mapping, which never carries acceptance across. `remap`
  records the mapping once per anchor, layer, and method version (now `resolve.v2`).
- Viewer routes in `api/routes/documents.py`: upload, document with pages and triage, page PNG at a
  scale (unrotated, cached in the artifact store), word boxes per page, create and get anchors.
- `acquire` walks a list of `FullTextLocation`s through the gateway, keeps the first readable PDF,
  stores source, licence, and terms URL, and writes `document.acquired` or
  `document.acquisition_failed` with every attempt.
- Fake: `kittum.documents.testing.TextParser` builds layers from plain text, no PDF needed.
- Fixtures: two CC BY 4.0 BMC Medicine RCTs (tests/documents/fixtures/ATTRIBUTION.md).

Open:

- Source resolvers that turn a record into `FullTextLocation`s (Europe PMC, Unpaywall, OpenAlex,
  publisher TDM) and the human upload queue; `acquire` takes the list.
- The upload route stores the document and its base layer but no acquisition: it has no project
  or actor yet (needs API auth and the Postgres ledger store).
- Ask Artifex where the PyMuPDF Layout source is. With it, `find_tables` could gate borderless
  tables itself; until then the caption-and-rules finder covers three-rule journal tables only.
- OCR on a machine with Tesseract; RapidOCR; PaddleOCR-VL adapters; GROBID metadata.
- `core.find_quote` either joins every hyphenated line break or none, so a quote that crosses a
  split word ("resid-" / "ing") and a compound broken at its hyphen ("middle-" / "income") in the
  same passage does not match. A change in `core`, not here.
- Docling runs per document (about 30 s per page on a CPU here); run it as a job, on pages with
  tables first.

## WP-11 Extraction

Module `kittum.extraction`. Needs: `documents`, `protocol`, `models`, `runs`.

- The extraction schema lives in `kittum.extraction` (`load_schema`, YAML or JSON): arms,
  comparisons, endpoints with timepoints and quantities (N randomized and analysed, events,
  percentages, means, effect estimates, CI bounds, p-values, follow-up, descriptions), each with a
  `critical`, `standard`, or `descriptive` policy and optional Janus locator hints.
  `check_against_outcomes` ties outcome endpoints to the protocol's outcome IDs; the protocol
  itself is unchanged. Done.
- The Janus path: a layer as Janus `DocumentEvidence` (lines as text elements, tables with
  folded header paths and group-row sections), one Janus specification per schema, Janus's
  locators and statuses, Janus's typed parser after normalizing minus signs and inline estimator
  labels, and every value anchored on its layer cell with `make_anchor`. Done.
- The LLM path: a PydanticAI agent on any `LlmBackend` (`FunctionModel`, one JSON
  Schema-constrained call per turn), read-only tools (search, read a page, open a table), a call
  and a time budget, and a verifier that anchors each quote with `quote_spans` and checks the
  value is printed in it. Document text is fenced as data. Done.
- Validators on both paths: exact values with units, events at most N, arms sum to the total, the
  CI brackets the estimate, percentages recompute, ratios are symmetric on the log scale. Done.
- Reconciliation per semantic key (agree, disagree, one side only) with the policy's gate,
  second-extractor flag, and the 10% re-check draw. Done.
- Runs are jobs (`request_run`, `run_handler`), published with the job through
  `Commands.execute`. `accept`, edit-and-accept with a source span, and `group_accept` write one
  `extraction.value_accepted` event each; `assertions` is the accepted set. A bundle exporter adds
  the tables and their anchors. HTTP routes in `api/routes/extraction.py`. Done.
- Acceptance: the Lesotho and Vitamin C trial PDFs with a real schema; Janus anchors checked by
  reading the PDF under the box; agent answers recorded from `qwen3.5:4b` on Ollama
  (`tests/extraction/record_fixtures.py`); planted validator errors; a real disagreement (the
  agent read 51 for the arm Table 1 prints as 52). Done.
- Open: a `kittum worker` entry point that registers `run_handler` with configured backends;
  grounding exceptions (a value read from a figure); the second, independent extraction a
  critical disagreement requires; the re-check queue itself (only the draw is recorded);
  re-anchoring accepted values when a project promotes a new parser.

## WP-12 Appraisal

Module `kittum.appraisal`. Needs: `documents`, `extraction`.

- Instruments as data (RoB 2, ROBINS-I V2, plus profile-registered forms); quote assembly per
  question.

## WP-13 Statistics

Module `kittum.stats`. Needs: nothing.

- Every function in the module, with scipy where it helps. Done.
- `clopper_pearson` defaults to one-sided bounds, as Kittum states every bound: 10 of 10 gives
  a lower bound of 0.741, and the two-sided 0.692 figure remains available with `sided="two"`.
- `elusion_estimate` is a stratified Horvitz-Thompson total with simultaneous one-sided exact
  hypergeometric bounds (Bonferroni over strata, exact on census strata). Sampling is without
  replacement, so the bounds invert the hypergeometric tail rather than a binomial one.
- `stop_test` implements Callaghan and Müller-Hansen (2020) with the suffix-minimum p-value from
  the reference implementation. Pass `order` for the exact reference result; without it the
  test uses the whole sequence and is conservative.
- `capture_recapture` uses Chapman for two sources with an exact hypergeometric overlap
  inversion (the upper end is unbounded when the overlap is zero), and a Poisson log-linear
  model without a three-way interaction for three sources. Zero observed cells make that model
  non-estimable and raise a `ValueError` naming the cell.
- `conformal_threshold` is a split-conformal order statistic with a binomial coverage guarantee;
  `recall_needed` gives its calibration size for zero misses.
- `cohen_kappa`, `fleiss_kappa`, and `pabak` cover two raters and three or more runs.
- Acceptance: tests against published worked examples, including 10 of 10 giving a 0.741
  one-sided (0.692 two-sided) lower bound, the 119-record case from DESIGN.md, the buscarpy
  stopping reference, and seeded simulations for every interval. Done.
- Open: the evaluation harness (WP-17) still has to pool these bounds across topics.

## WP-14 Reports

Module `kittum.report`. Needs: most modules.

- The core targets: PRISMA 2020 flow per scope, RIS exports, the handover bundle, and the AI
  disclosure. Profile targets register through `report.register_target`; the `gba_amnog`
  targets are listed in docs/profiles/gba-amnog.md.
- Acceptance: golden files for a small fixture project.

**Done.** Every target renders from `load_review(tx, project_id, cutoff_seq=...)`: the review
as the ledger recorded it through a cutoff (default: the head). Screening is replayed from the
append-only decision, undo, and adjudication rows, and conflicts and extra screens from their
ledger events (`screening.conflict_*`, `screening.adjudicated`,
`screening.ai_disagreement_rescreen`), so a report at an old cutoff shows the review as it stood
then. A record sent to the auditor's blind extra screen stays unresolved until the auditor
decides. Migration `20261029020000_report_artifacts.sql`.

- `prisma`: `flow(review, topic, scope)` from one frozen identity revision (default: the latest
  at the cutoff). Identification counts the occurrences of the topic's searches that revision
  holds; records of searches run after it are reported apart (`records_outside_identity`). Each
  topic's flow is self-contained, as PRISMA reports one per question: a report several topics
  found is identified and unique in each. So it equals `corpus.get_counts` (the whole revision)
  only when one topic was searched; with several, the topics' identified records sum to its
  total and the union of their unique records is its reports. Records per source and per kind (databases, registers, other),
  duplicates, screened and awaiting screening (screened as screening counts it: every screener
  decided, and the auditor of an extra screen), excluded, sought, not retrieved (sought but not
  in the full-text stage), assessed and awaiting assessment,
  exclusions per criterion ID, awaiting classification (insufficient information), included
  reports and studies. `members` lists the reports behind every count; `checks` states every sum
  that must hold and reports a broken one. The PRISMA automation box is always 0 (the AI never
  removes a record); exclusions with an AI proposal, after a reveal, or after an extra screen the
  AI's disagreement triggered are counted separately. JSON and an SVG diagram per scope.
- `ris`: one undeduplicated file per search, the deduplicated corpus (one entry per report), and
  per scope the included, title-and-abstract excluded, and full-text excluded lists. `AN` is the
  provider ID, `N1` the registry IDs and decision, `C1`/`C2`/`C3` the occurrence, report, and
  study. Fields are written on one line (RIS cannot carry line breaks); the bundle keeps the text.
- `search_report`: every search with platform, dates, the strategy as run, limits, study filter,
  the query translation, reported and retrieved counts, raw hashes, and who ran it; planned
  sources without a search and source deviations.
- `ai_disclosure`: the disclosure record (`disclosure(review)`: recipes, weights hashes and
  licences, the identities the server reported, prompt and schema hashes, decoding, mode per
  topic with rationale and validation pack, reveal per stage, kappa and PABAK against the model's
  own and the verified reading, reveals, adjudicated conflicts, the audit gauge, the extra screens
  the AI triggered with each one's reason and result (upheld, adjudicated, pending),
  `judge.VERIFIER_LIMITS`) and one RAISE-aligned Markdown rendering.
- `render(tx, target, project_id, scope_id, store=...)` stores the files by hash and returns an
  `Artifact`; `record_render` writes `report_artifact` and a `report.rendered` event.
- `seal_bundle(tx, project_id, store, out_dir, signer=..., actor_id=..., rights=...)` renders
  every core target into `reports/<target>/` and the protocol's profile targets into
  `reports/dossier/<name>/`, at the bundle's cutoff, and seals with every registered exporter.
  `kittum.report` registers exporters for the protocol, search, corpus, judge, screening, and
  report tables (`report/exporters.py`), each filtered to rows whose event is at or before the
  cutoff; mutable columns are left out or recomputed. A `RightsProfile` withholds raw files of
  named sources or hashes; a referenced artifact missing from the store is listed by hash with
  that reason.
- `gba_amnog` targets in `kittum.profiles.gba_amnog.reports`, registered by `load_profiles`:
  `anhang_4a`, `anhang_4b`, `anhang_4c`, `anhang_4d`, `justification_4235`, `anlage_ii_1`
  (Markdown and CSV, RIS for the tree).
- Routes in `api/routes/report.py`: `GET .../reports/targets`, `POST .../reports/targets/{target}/render`,
  `GET .../reports/artifacts/{sha256}` (only files rendered for the project), `POST .../reports/bundle`.
  Rendering, downloading, and sealing are for the methods lead only (`report.require_lead`): a
  human who screens, audits, or adjudicates no stage of the project, the rule `Screening.status`
  uses for the lead's view. Everyone else gets 403.
- Changes outside the module: `BundleWriter.file` in `ledger/bundle.py` (exporters add files the
  manifest lists); `judge.VERIFIER_LIMITS`; `ALLOWED` lets `report` import `judge` and
  `profiles` import `report`.
- Tests (`tests/report/`, `tests/profiles/test_gba_amnog_reports.py`) build the nicotine
  replacement review and the capivasertib dossier through search, corpus, judge, and screening
  against Postgres, and check the PRISMA sums by hand and from data, earlier cutoffs, the RIS
  round trip, the PRISMA-S fields, the disclosure, the sealed bundle through `kittum verify`, the
  trace from report numbers to decisions, rescreen events, and people in the bundle's SQLite, the
  routes, and each copied metric against `Screening.status`.

Left open:

- The PRISMA target has no update-search delta and no methods text yet (DESIGN.md "Reports").
- The Anlage II.1 file names are provisional (`ANLAGE_II_1`, and `GAPS.md` in the output); the
  profile doc does not record them. The tree has no PDFs until WP-10. `gba_amnog.modul4` (the
  Modul 4 text and tables, Anhang 4-E and 4-F) and `gba_amnog.jca` wait for extraction and
  appraisal.
- Anhang 4-A shows the strategy line by line, with the hit count only on the last line: searches
  do not record per-line counts.
- `kittum verify` does not yet re-run the count reconciliation; the `checks` in each flow do it at
  render time. `registry/` (lockfiles, SBOM, parser recipes) is not in the bundle; model recipes
  and prompt hashes are, in `judge_config`.
- The exporters live in `report` and read the other modules' tables directly; an owner may move
  its exporter into its own package. Enrichment rows are not exported.
- A proposal's status is not replayed to the cutoff (it is left out of the bundle); the
  confirmations are ledger events.
- Screened counts, open and resolved conflicts, agreement, reveals, the audit gauge, and the
  rescreen gauge are recomputed from the replayed rows with `screening.metrics`'s definitions, so
  they stay exact at old cutoffs. `test_a_metric_the_report_computes_equals_what_screening_reports`
  compares each with `Screening.status` on both reviews, so a change there fails here by name.
- The report routes take the actor in the body or the `X-Kittum-Reviewer` header until WP-15 adds
  authentication; the lead check is only as good as that stand-in.

## WP-15 API

Modules `kittum.api`, `kittum.app`, and `kittum.people`. Needs: the modules it exposes.

- Routes for the workbench screens, thin over module services. Regenerate contracts after each
  change.

**Done** (branch `kittum/wp-15-integration`, on main with WP-09, WP-11, and WP-14):

- `kittum.app` is the composition root, an entry point like `api` and `cli` (`"app": {"*"}` in
  the architecture test). `AppContext` moved there; `kittum.api` re-exports it.
  `AppContext.from_settings` builds the Postgres ledger, the job queue and command receipts
  (extraction's commands use them), the filesystem artifact store, the `SourceGateway` (default
  policies, `HttpxTransport` with the contact email), every model backend in
  `Settings.llm_endpoints` by recipe, and the screening `Judge`. A hosted judge recipe is refused
  at start (screening records are `local_only`); the backends refuse each call as before.
- Settings: `KITTUM_JUDGE_MODEL`, `KITTUM_JUDGE_BACKEND` (`endpoint`, or `demo` to replay the
  demo's recorded answers), `KITTUM_WEB_DIST`; all listed in `kittum/.env.example`.
- `kittum serve` serves the API and, with `KITTUM_WEB_DIST` or `--web-dist`, the built workbench
  (files under `/assets` immutable, `index.html` for every other non-API path).
  `kittum worker` runs a `Worker` with every registered job kind (`register_job_handler(kind,
  factory)`): the screening judge, and extraction's `run_handler`. Without a model server an
  extraction run takes the Janus path; one that asks for the agent gets LLM proposals recorded
  as errors that name the missing backend. A judge worker
  refuses, before any model call, the jobs of a stage that names another judge config.
- `kittum.people` owns who works on a project and in which roles (lead, reviewer, auditor,
  adjudicator; migration `20261029040000_app_projects.sql`, table `project_member`); screening
  and report may import it (the `ALLOWED` table). `kittum.screening.sees_lead_data` is the one
  rule for what only the lead sees: a project lead who screens, audits, or adjudicates no stage.
  `Screening.status`'s whole-stage view (anyone else outside the stage gets 404),
  `report.require_lead`, the catalogue routes below (403), and progress's identification counts
  and outcomes all use it, so a lead who also screens is a screener first.
- The project lifecycle (`kittum.app.lifecycle`), each step a function on the caller's
  transaction with a ledger event, a route, and a `kittum project` command: create a project and
  its lead, add people with roles, publish a protocol, run or import
  searches (the search routes), ingest the corpus (once; update searches need the corpus diff),
  attach full texts (table `full_text`, event `project.full_text_attached`), open a stage, and
  progress. A stage takes its screeners, auditor, and adjudicators from the people; title and
  abstract screens every report of the newest identity revision; full text every record included
  at title and abstract that has a full text, with sentinels from the project's sentinel full
  texts (`attach_sentinel_full_text`). Opening a stage enqueues the judge. `kittum project
  open-stage --sentinels FILE` takes title and abstract sentinels from a YAML or JSON list
  (`examples/sentinels/nicotine-replacement.yaml`, which the demo uses too); a refused step
  prints why and exits 1.
- `kittum demo seed [--live] [--no-wait] [--json]`: the nicotine replacement example (no
  profile), a PubMed search replayed from a recording (`scripts/record_demo_search.py`, 20
  records), an example CENTRAL export with one duplicate of a PubMed record (confirmed by the lead
  in the seed), a citation search with the two CC BY trials and a registry entry that shares the
  Lesotho trial's NCT number (a pending study link for the Studies screen), both PDFs as full
  texts, the Vitamin C PDF as a full-text sentinel, five people, and an open title and abstract
  stage. Recorded answers (`demo_data/judge_answers.json`) are what `qwen3.5:4b` on Ollama
  answered (`scripts/record_demo_answers.py`).
- Server additions for the workbench, each in its module's route file:
  - `projects.py`: `GET/POST /api/projects`, `GET /api/projects/{id}`, `POST .../members`,
    `POST .../full-texts/{item_ref}` (PDF body), `GET .../progress` (each stage the viewer has a
    part in, as they may see it; identification counts and outcomes for the lead only).
  - `ledger.py`: `GET .../ledger/verify` (the report and event counts by type; events themselves
    are not served, since their payloads hold every reviewer's decisions).
  - `screening.py`: `GET/POST .../screening/stages`, `GET .../assignments/{id}/full-text` (the
    document, layer, and protocol-term mentions per criterion, from `kittum.app.fulltext`). Rule
    violations (422) now use FastAPI's validation-error shape, so the generated client decodes
    them.
  - `corpus.py`: `POST .../corpus/ingest`, `GET .../corpus/revisions/latest`. That, the link
    proposals, and `search.py`'s executions take `X-Kittum-Reviewer` and answer 403 to anyone
    but a project lead: a reviewer who could list every record could tell the sentinels in their
    queue by their absence.
  - `protocol.py`: `GET .../protocol` (the published version with the profile's labels for core
    concepts, the core's words otherwise).
  - `documents.py`: `POST /api/layers/{id}/anchors` takes `start` to anchor a mention or a
    selection at its offset.
- Module additions the wiring needed: `corpus.latest_revision` and `corpus.reports` (the read
  WP-09 asked for; `CorpusReport` carries the source records); `Screening.stages`, `.stage`, and
  `.assignment_source` (the item reference stays server-side, since a sentinel's names it);
  `documents.derive` restores a layer whose JSON the artifact store lacks (from the parsed layer
  this process still holds, or by parsing again), and refuses if the output hash differs (a
  database paired with another store used to fail on read).
- Tests: `tests/app/test_lifecycle.py` drives a project from nothing to an open stage over HTTP,
  with the judge-mismatch refusal, and a third reviewer outside the stage who gets no catalogue,
  no stage, and no reports, and a lead who also audits and gets none of the lead's data either;
  `tests/app/test_worker.py` runs extraction jobs with no model server;
  `tests/app/test_cli.py` sets a review up from the command line; the
  end-to-end journey (WP-16) covers the rest on the real stack.

Open:

- Authentication. `X-Kittum-Reviewer` (screening, progress) and the `actor_id` fields in request
  bodies are stand-ins: anyone who can reach the API can act as anyone. The workbench says so.
- Identity revisions after a stage opens (a confirmed link, a merge) do not reach the open stage,
  which keeps the reports it opened with. Update searches need the corpus diff (WP-07).
- One judge per deployment: a worker runs one judge config, and opening a stage uses it. A
  stage with another config needs a worker started with it; a mismatched worker fails the jobs
  for good (they need requeueing by hand).
- The full-text waterfall's automatic sources (WP-10) should attach through `attach_full_text`;
  only uploads and the demo do now. Full-text sentinels need a PDF each.
- The extraction screen is not wired to WP-11's routes yet: `Workbench.extraction` and `accept`
  in `apps/web/src/data/api.ts` return "not served yet".
- Writes (confirm, merge, split, render, seal) name their actor in the request body; only
  reports check that it is a lead.
- `kittum serve` runs one uvicorn process; production needs a process manager and TLS in front.

## WP-16 Workbench

`apps/web`. Needs: WP-15.

- Screens from DESIGN.md "Reviewer workbench", starting with the title and abstract queue and its
  keyboard flow. Base UI for components, Legend List for queues, server-rendered PDF pages with
  word-box text layers for full text.

Done (branch `kittum/wp-16-workbench-design`, then `kittum/wp-15-integration`):

- Design system: tokens for light and dark, IBM Plex Sans with its condensed width, keycaps, the
  status vocabulary, and the evidence marker that pairs a selected item with its source.
  `apps/web/README.md` describes it.
- `Workbench` (`src/data/workbench.ts`) has two implementations: `src/data/api.ts` on the
  generated client in `@kittum/contracts` (development and production) and the mock on recorded
  fixtures (unit tests; `VITE_KITTUM_WORKBENCH=mock` for a server-less look). Calls that depend on
  who asks take the reviewer's actor ID, sent as `X-Kittum-Reviewer`. Server records come typed
  from the contracts; `src/data/schema.ts` keeps only what the screens compose.
- Who is at the workbench: a picker of the project's people, remembered per tab (two reviewers
  side by side in two tabs), `?as=` on any URL, and a header chip to switch. It says plainly that
  there is no sign-in. Each person starts where their work is.
- Title and abstract queue on the server: five prefetched, decide, reveal on disagreement after
  the reviewer's own decision, keep or revise, Backspace reopens and a new decision undoes the old
  one on the server, an item awaiting a final decision comes back with its reveal, and a queue
  that waits for the AI judge says so instead of "finished". The auditor screens here too.
- Full text on the server: page PNGs and word boxes from the documents routes, protocol-term
  mentions per criterion, `Enter` saves a mention or a selection as an anchor, the decision
  carries a reading and evidence per criterion, the AI's reading after a provisional decision
  (keep with `Space`), and the next full text.
- Adjudication: the record with the pseudonymized columns side by side, `1`/`2` adopt, `i` `e`
  `u` own decision, the AI column after the commit, `Space` next.
- Progress: the PRISMA-style flow (identification by source, duplicates, per stage screened,
  excluded, in conflict, waiting, included; full texts; studies), and for the lead agreement
  (kappa, PABAK; human pairs and human-AI), reveals, extra screens, sentinels, the audit gauge
  with the elusion bound, and the ledger check; `n` opens full-text screening for a scope.
  Reviewers see their own counts only.
- Protocol (read only, with profile labels), Search (executions, counts, line-numbered
  strategies, the platform's translation), Studies (proposals first; `Enter` confirm, `x` mark,
  `m` merge, `s` split). Search and Studies are the lead's: anyone else sees why, not the records.
- Reports (the lead's): `p` renders the PRISMA 2020 flow from the ledger and shows its counts and
  diagram per scope, `s` seals the handover bundle and says how to verify it offline.
- End to end: `apps/web/e2e/review.spec.ts` (Playwright, `pnpm --dir apps/web e2e`, CI job
  `kittum-e2e`) seeds the demo and starts `kittum serve` and `kittum worker` with the recorded
  judge, then runs one journey: reviewer A screens by keyboard with an undo and a reveal (nothing
  of the AI on screen before A decides); reviewer B screens and every screening response B
  received is checked for A's ID and for AI rationales outside B's own reveals; the auditor gets
  the AI-only disagreements; the adjudicator decides with no AI output on the page and then sees
  it; the lead checks the flow, the excluded and included counts, conflicts, extra screens, and
  kappa, and opens full text; both reviewers and the auditor screen the real Lesotho PDF and the
  full-text sentinel with adopted evidence; the lead confirms the study link; studies drop from 29
  to 28, the full-text extra screen is upheld, and the ledger verifies in the UI and with
  `kittum ledger verify`. Reviewer B and the auditor get 403 from the catalogue and no lead
  figures, and no screening or progress answer they received has an AI verdict field, another
  person's decision, or a reveal outside their own decision. The lead renders the PRISMA flow
  (the same counts as Progress) and seals a bundle that `kittum verify` accepts.
- Browser unit tests in Vitest browser mode on the mock, and screenshots in `apps/web/screenshots/`.

Left:

- Extraction review on the server (WP-11), the other report targets on the Reports screen, and
  the Risk of bias and AI-exclusion audit screens; the full-text judge's per-criterion reading in adjudication; grounding exceptions.
- Adjudication shows a full-text column's evidence as counts, not the quotes on the page.
- Opening stages other than full text, adding people, and publishing a protocol happen in the CLI
  or the API; the workbench has no forms for them yet.
- A session break suggestion after a missed sentinel or a speed-up with an include-rate change.
- Third-party licence notices for the web bundle (`licenses:sync`).
- `scripts/screenshots.ts` still shoots the mock; point it at a seeded server.

## WP-17 Evaluation harness

Not a runtime module; lives in `apps/server/eval/`.

- Loaders for SYNERGY and the gold-data tiers G1 to G3 (DESIGN.md "Evaluation"), a runner that
  scores judges and extraction, and a report.
