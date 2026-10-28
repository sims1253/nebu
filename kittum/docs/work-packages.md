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
| 01 | `ledger` | Chain definition and in-memory fake done; stores open |
| 02 | `runs` | Types done; queue open |
| 03 | `protocol` | First-cut types; open |
| 04 | `gateway` | Done: httpx transport, policies, shared buckets, budgets, cache, attempts; AppContext wiring open |
| 05 | `search` | Types done; open |
| 06 | `enrich` | Types done; open |
| 07 | `corpus` | Types done; open |
| 08 | `judge` and `models` | Types and recorded backend done; open |
| 09 | `screening` | Types done; open |
| 10 | `documents` | Types done; open |
| 11 | `extraction` | Types done; open |
| 12 | `appraisal` | Types done; open |
| 13 | `stats` | Signatures done; open |
| 14 | `report` | Types done; open |
| 15 | `api` | Health route done; open |
| 16 | Workbench | Shell done; open |
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

## WP-02 Runs and jobs

Module `kittum.runs`. Needs: `ledger`.

- `JobQueue` on the `job` table with `for update skip locked`, leases, heartbeats, retries with
  backoff, and dead-lettering. Unique `stage_key` prevents double publication.
- Command receipts: a retried command returns the stored receipt and writes nothing.
- The decider pattern: a helper that commits events, rows, receipt, and jobs in one transaction.
- A worker loop with graceful shutdown, and an in-memory queue fake.
- Acceptance: two workers never run the same job; a crashed worker's job is reclaimed after its
  lease; a replayed command is a no-op.

## WP-03 Protocol

Module `kittum.protocol`. Needs: `core`.

- The generic schema from DESIGN.md "Protocol" (review questions and scopes, PICO, comparators,
  outcomes, effect modifiers, source plan, reference set, audit and stopping plans, freshness
  policy), with a profile registry for extensions. The `gba_amnog` dossier fields live in
  `kittum.profiles.gba_amnog`.
- `compile(source)` from JSON or YAML with clear validation errors; a content hash per version.
- `diff(a, b)` for criteria, scopes, and modes.
- Criterion rendering: the human question and the model prompt fragment.
- Acceptance: a worked example protocol in `tests/protocol/fixtures/`, round-trip stable hashes.

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
  translation capture.
- ClinicalTrials.gov API v2.
- File imports: RIS, Cochrane Library export, Embase and Ovid exports with search history, CTIS
  and EU-CTR exports.
- A `FakeSource` in `search/testing.py`.
- Acceptance: recorded responses in `tests/search/fixtures/` reproduce counts and PRISMA-S fields.

## WP-06 Enrichment

Module `kittum.enrich`. Needs: `gateway`, `search`, `models`.

- OpenAlex, Crossref (retractions through `update-to`, `clinical-trial-number`), Europe PMC,
  Unpaywall, PubTator 3, with "first healthy source" and batched lookups.
- Local entity tagging with the OpenMed GLiNER models, for text that must stay local.
- Acceptance: recorded responses; a retracted DOI is flagged.

## WP-07 Corpus

Module `kittum.corpus`. Needs: `search`, `enrich`, `ledger`.

- Dedupe rules (DOI, PMID, registry ID, normalized title, year) that keep every occurrence.
- Link proposals for report and study with evidence strings.
- Identity revisions written as ledger events; PRISMA counts from a frozen revision.
- Acceptance: a fixture with duplicates, a multi-report study, and a conference abstract.

## WP-08 Judge and models

Modules `kittum.judge` and `kittum.models`. Needs: `protocol`, `core`.

- An OpenAI-compatible backend for vLLM and Ollama with JSON Schema-constrained output; the model
  recipe registry with the licence allowlist; the confidentiality check.
- `judge`: the prompt per criterion set, decoding, `find_quote` verification, keyword and negation
  checks, downgrade to `unclear`, raw output capture.
- Gold-data metrics: per-judge recall and specificity, the double-fault rate, run-to-run kappa.
- Acceptance: recorded completions cover verified, unverified, paraphrased, and negated quotes.

## WP-09 Screening

Module `kittum.screening`. Needs: `protocol`, `corpus`, `judge`, `ledger`, `runs`, `stats`.

- Assignments per mode (M1 to M3), blinding enforced in queries, reveal-after-commit with
  provisional and final decisions, sentinels and audit samples hidden from reviewers, conflicts,
  adjudication.
- Acceptance: a reviewer can never read the other reviewer's decision or the AI verdict before
  committing; tests prove it at the service boundary.

## WP-10 Documents

Module `kittum.documents`. Needs: `gateway`, `ledger`.

- Page triage with pdf-inspector; parsers for PyMuPDF, pypdfium2, and Docling behind `Parser`;
  OCRmyPDF for scans; NFC canonical text with word boxes in PDF points.
- Layer storage by content hash; `resolve` with exact, candidate, ambiguous, and unresolved.
- The full-text waterfall (docs/sources.md).
- Acceptance: fixture PDFs with multi-column text and a merged-header table; anchors survive a
  re-parse with a different engine.

## WP-11 Extraction

Module `kittum.extraction`. Needs: `documents`, `protocol`, `models`.

- The Janus adapter: layer to Janus `DocumentEvidence`, a specification per endpoint schema.
- The PydanticAI agent with read-only tools and a call budget.
- Validators (events at most N, arms sum, CI brackets the estimate, percentages recompute) and
  reconciliation between paths.
- Acceptance: fixture tables give the same values on both paths; validators catch planted errors.

## WP-12 Appraisal

Module `kittum.appraisal`. Needs: `documents`, `extraction`.

- Instruments as data (RoB 2, ROBINS-I V2, plus profile-registered forms); quote assembly per
  question.

## WP-13 Statistics

Module `kittum.stats`. Needs: nothing.

- Every function in the module, with scipy where it helps.
- Acceptance: tests against published worked examples, including 10 of 10 giving a one-sided 95%
  lower bound of 0.741 (0.692 two-sided) and the 119-record case from DESIGN.md.

## WP-14 Reports

Module `kittum.report`. Needs: most modules.

- The core targets: PRISMA 2020 flow per scope, RIS exports, the handover bundle, and the AI
  disclosure. Profile targets register through `report.register_target`; the `gba_amnog`
  targets are listed in docs/profiles/gba-amnog.md.
- Acceptance: golden files for a small fixture project.

## WP-15 API

Module `kittum.api`. Needs: the modules it exposes.

- Routes for the workbench screens, thin over module services. Regenerate contracts after each
  change.

## WP-16 Workbench

`apps/web`. Needs: WP-15.

- Screens from DESIGN.md "Reviewer workbench", starting with the title and abstract queue and its
  keyboard flow. Base UI for components, Legend List for queues, server-rendered PDF pages with
  word-box text layers for full text.

## WP-17 Evaluation harness

Not a runtime module; lives in `apps/server/eval/`.

- Loaders for SYNERGY and the gold-data tiers G1 to G3 (DESIGN.md "Evaluation"), a runner that
  scores judges and extraction, and a report.
