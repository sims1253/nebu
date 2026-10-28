# Sources

This page lists the search and enrichment sources Kittum uses, their limits, and their terms for
commercial use. "Checked" means a council member read the provider's current page in October 2026.
"Unverified" means nobody confirmed the value. Confirm unverified values before the spike, and
store the terms URL and the date read with each acquisition.

Free access, commercial use, metadata rights, and PDF redistribution are four separate questions.
A source can answer yes to one and no to another.

## Roles

| Role | Counts toward PRISMA identification | Load spreading allowed |
| --- | --- | --- |
| Primary search | Yes, one `SearchExecution` per database per topic | No. A strategy runs on its own database only. |
| Other methods (citation chasing, agency website lookups, the sponsor's study list) | Yes, in the "other methods" column | No |
| Enrichment (abstracts, DOIs, OA locations, retractions, entities) | No | Yes |
| Full-text acquisition | No | Yes, as a waterfall |

## Minimum sources

The review's method guidance, or its [profile](../DESIGN.md#profiles), sets the minimum list of
sources in its `SourcePlan`. A common baseline for reviews of drug interventions is:

- Bibliographic databases: MEDLINE, including in-process citations, and Cochrane CENTRAL. Further
  databases such as Embase are often expected.
- Registries: ClinicalTrials.gov, EU-CTR, CTIS, and the EMA Clinical Data portal. For EMA Clinical
  Data, a lookup of studies already identified is usually enough.

Some method guidance excludes routine searches of preprint servers, and a profile can enforce that.
Preprints found another way (citation chasing, Europe PMC, the sponsor's study list) still go
through normal screening and quality assessment.

A profile can add sources, such as a lookup on an agency's own website. Each profile's doc lists
its minimum.

## Free sources

| Source | Kittum role | Limits | Access | Commercial use | Notes |
| --- | --- | --- | --- | --- | --- |
| PubMed E-utilities | Primary search (MEDLINE) | 3 requests per second without a key, 10 with a free key. Checked. ESearch returns at most 10,000 records per query. | Free NCBI key, plus `tool` and `email` parameters | Allowed for metadata. Abstracts keep publisher copyright for redistribution. | Returns the query translation, which goes into the PRISMA-S record. Kittum splits large searches by date window to stay under the cap and logs each window. |
| ClinicalTrials.gov API v2 | Primary registry search | About 50 requests per minute. Unverified. | None | US public domain | Essie syntax with `AREA[]` and `RANGE[]`. Includes structured results. |
| Europe PMC REST | Optional extra database; full-text XML source | Fair use, about 10 requests per second. Unverified. | None | Metadata allowed. Full-text licence per article. | Set `synonym=FALSE` so hit counts are reproducible. Best free source of JATS full text, with sections and tables already structured. |
| OpenAlex | Enrichment and citation chasing | A free key is required since February 2026. The free key gets a budget of 1 USD per day. Complex Boolean queries have lower limits. Checked against the current help pages. | Free key | CC0 metadata | Use the free CC0 snapshot for bulk work. Pin the policy version and read the response headers. |
| Crossref | DOI resolution, retraction notices, trial-number links | Public pool 5 requests per second at concurrency 1. Polite pool 10 per second at concurrency 3. Checked. | `mailto` parameter | Metadata allowed | `update-to` carries retractions and corrections. `clinical-trial-number` links papers to registry IDs. |
| Unpaywall | OA PDF location | 100,000 calls per day. Checked. | `email` parameter | Allowed | The linked PDF's licence is a separate check. |
| PubTator 3 | Entity tags for highlighting and dedupe signals | 3 requests per second. Unverified. | None | NCBI public service | Annotations never decide eligibility. |
| Semantic Scholar | Not used in the commercial profile | 1 request per second with a key | Free key | Restricted. The default licence requires extra permission for commercial use. Checked. | Allowed only in a non-commercial research profile. |
| CORE | Not used in the commercial profile | Tiered | Free key | Treat as restricted until checked. A council member reported that consultancy use needs a paid licence. | Optional, off by default |
| Lens.org | Not used in the commercial profile | Trial plans are small | Token | A council member reported that the terms class consultancy reports as commercial use. | Optional, off by default |
| medRxiv and bioRxiv | Supplementary only | About 1 request per second | None | Per-article CC licence | No Boolean search. Search through Europe PMC (`SRC:PPR`) instead. |

## Sources without a free search interface

| Source | Why it matters | Kittum adapter |
| --- | --- | --- |
| Cochrane CENTRAL (Wiley) | Common minimum | `FileImport` of the Cochrane Library export and strategy text. Wiley's TDM token is for non-commercial use. |
| EU-CTR | Common minimum; an archive since 2025 | `FileImport` plus a captured copy of the search page |
| CTIS | Common minimum | `FileImport` plus a captured copy. No documented public API was found. Kittum does not scrape it without an EMA terms review. |
| EMA Clinical Data portal | Results of identified studies | A per-study lookup with free registration |
| WHO ICTRP | Some method guidance requires it, often only for non-drug interventions | `FileImport` of the portal export |
| Epistemonikos | Cross-check against existing reviews | `FileImport` of a RIS export |

## Commercial sources

Each commercial database gets two adapters. An API adapter works when the contract grants automated
access. An import adapter works always: Kittum compiles the strategy into the platform's dialect and
gives the search specialist a runbook. It then ingests the export together with the platform's
search history and checks the line-by-line hit counts.

| Source | Interface | Dialect notes |
| --- | --- | --- |
| Embase (Elsevier) | Embase API under an institutional key, or Embase.com export | Emtree terms (`/exp`, `/de`), `:ti,ab,kw`, `NEAR/n` |
| Ovid (MEDLINE, Embase, EBM Reviews) | Saved search plus export | Numbered lines, `exp Term/`, `.tw,kf.`, `adj3`. Kittum parses the exported search history into PRISMA-S rows. |
| Cochrane Library (Wiley) | Search Manager export | `[mh "Term"]`, `:ti,ab,kw`, `NEAR/n` |
| Scopus and ScienceDirect TDM (Elsevier) | Scopus Search API, Article Retrieval API | `TITLE-ABS-KEY()`, `W/n`. Useful for citation chasing and licensed full text. |
| Web of Science (Clarivate) | Starter or Expanded API | `TS=()`, `NEAR/n`, cited-reference endpoints |
| Publisher TDM (Wiley, Springer Nature, Elsevier) | Token-based full-text interfaces | Full-text acquisition only |

Quotas for commercial interfaces vary by contract. Kittum reads them from configuration, never from
code.

## How Kittum spreads load

All outbound HTTP goes through one gateway inside `enrich` and `search`:

- A token bucket and a concurrency cap per source. Kittum persists the counters in Postgres, so a
  restart does not reset a daily budget such as OpenAlex's or Unpaywall's.
- A safety margin of about 10% under every published limit.
- The gateway reads `Retry-After` and rate-limit headers and slows down before a 429 response.
- Retries with exponential backoff and full jitter on 429, 502, 503, and 504, up to five attempts.
  The ledger records every attempt.
- A content-addressed response cache keyed by the request hash. Replaying an existing execution
  makes no network calls. A new execution of the same strategy, such as an update search, bypasses
  the cache and stores its own response snapshot.
- For enrichment, a "first healthy source" policy. A DOI lookup goes to Crossref, OpenAlex, or Europe
  PMC, whichever has budget and answers. Batched ID lookups (50 to 500 IDs per call where the source
  allows) cut call counts.

Federation never splits a primary strategy across sources. A primary search on a source that is out
of budget waits.

## Full-text waterfall

For each record that passes title and abstract screening, Kittum tries in order:

1. The local content store, by SHA-256, DOI, or PMID.
2. Europe PMC full-text XML and PDF.
3. Unpaywall and OpenAlex OA locations.
4. Publisher PDFs: the links a publisher deposits with Crossref where Crossref also lists a
   Creative Commons licence for that version, then licensed publisher TDM interfaces.
5. The human upload queue, for closed-access articles and client CSRs.

Kittum records the source, the licence, and the acquisition time for every file.
