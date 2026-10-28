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
| Europe PMC REST | Primary search (live, `cursorMark` paging); enrichment; full-text XML source | Fair use, about 10 requests per second. Unverified. | None | Metadata allowed. Full-text licence per article. | Set `synonym=FALSE` so hit counts are reproducible. Best free source of JATS full text, with sections and tables already structured. A PMID lookup is `EXT_ID:<pmid> AND SRC:MED`; a DOI lookup is not scoped, so preprints match, and a DOI can also match a PMC copy without a PMID or DOI, which Kittum ignores. On 28 October 2026 the search service answered every request with HTTP 503 for hours; **Retry failed sources** asks it again later. |
| PubMed Central (PMC ID converter and PMC Article Datasets on AWS) | Full-text lookup | The ID converter takes NCBI's E-utilities rate (3 requests per second without a key). The `pmc-oa-opendata` bucket publishes no limit; Kittum uses the converter's. Checked. | `tool` and `email` parameters for the converter; the bucket is world-readable | Per-article licence (`license_code` in the article's metadata JSON, mostly Creative Commons, `TDM` for author manuscripts without one) | PMC retired its OA web service (`oa.fcgi`) in August 2026, and its terms allow automated retrieval only through the Cloud Service, OAI-PMH, E-utilities, and BioC. Kittum finds the PMCID (from Europe PMC, else the converter), lists the bucket for the article's versions, reads each version's metadata, and fetches the PDF over HTTPS. The published version comes before an author manuscript. |
| OpenAlex | Enrichment and citation chasing | A free key is required since February 2026. The free key gets a budget of 1 USD per day. Complex Boolean queries have lower limits. Checked against the current help pages. | Free key | CC0 metadata | Use the free CC0 snapshot for bulk work. Pin the policy version and read the response headers. |
| Crossref | DOI resolution, retraction notices, trial-number links | Public pool 5 requests per second at concurrency 1. Polite pool 10 per second at concurrency 3. Checked. | `mailto` parameter | Metadata allowed | `update-to` carries retractions and corrections. `clinical-trial-number` links papers to registry IDs. |
| Unpaywall | OA PDF location | 100,000 calls per day. Checked. | `email` parameter | Allowed | The linked PDF's licence is a separate check. `url_for_pdf` can name the wrong file: for 10.2196/66709 it names the trial's CONSORT checklist, a supplement (issue #60). Kittum checks every PDF against its record before it becomes the full text. |
| PubTator 3 | Entity tags for highlighting and dedupe signals | 3 requests per second. Unverified. | None | NCBI public service | Annotations never decide eligibility. |
| Semantic Scholar | Full-text lookup (`openAccessPdf`) | 1 request per second with a key. Without one, a pool shared by every caller without a key, which answers 429 often. Checked. | Optional free key (`KITTUM_SEMANTIC_SCHOLAR_API_KEY`, sent as `x-api-key`) | Restricted. The default API licence requires extra permission for commercial use. Checked. | Asked by DOI, else PMID, after the preprint servers. Its `openAccessPdf` is often a landing page, and for 10.2196/66709 the same supplement Unpaywall names. A commercial deployment needs Semantic Scholar's permission. |
| CORE | Full-text lookup (repository copies) | Registered personal key: 1,000 tokens a day, 25 a minute. Academic key: 5,000 a day, 10 a minute. A simple query costs 1 token. Kittum uses 10 a minute and 900 a day. Checked. | Key required (`KITTUM_CORE_API_KEY`, sent as a bearer token). Full texts are not available without one. | Treat as restricted until checked. A council member reported that consultancy use needs a paid licence. | Asked last, by DOI, only with a key. Without one the waterfall skips it and the retrieval run says so. CORE states no licence for the copies it aggregates. Not recorded in Kittum's fixtures, because no key may reach a recording. |
| Lens.org | Not used in the commercial profile | Trial plans are small | Token | A council member reported that the terms class consultancy reports as commercial use. | Optional, off by default |
| medRxiv and bioRxiv | Supplementary search; full-text lookup | No published limit. Kittum uses 1 request per second. | None | Per-article licence in the `details` answer (`cc_by`, `cc_by_nc_nd`, `cc0`, `cc_no` for no reuse licence) | No Boolean search. Search through Europe PMC (`SRC:PPR`) instead. Full text: `api.biorxiv.org/details/<server>/<doi>` for a DOI 10.1101/..., medRxiv first, then bioRxiv; the newest version's PDF is fetched. The servers sit behind Cloudflare, which often refuses automated PDF downloads with 403. |
| arXiv | Full-text lookup | One request every three seconds, on one connection. Checked. | None | Per-paper licence in the OAI-PMH `arXiv` record | Asked for a DOI 10.48550/arXiv.<id>. The licence comes from `oaipmh.arxiv.org`, the PDF from `export.arxiv.org`, the host arXiv asks automated clients to use. |

## Sources Kittum does not use

| Source | Why not |
| --- | --- |
| ResearchGate | It has no public API, and its terms of service forbid automated downloads and scraping. Kittum never fetches from it. A PDF a reviewer gets from ResearchGate by hand goes through the upload, with its licence. |
| Sci-Hub and similar shadow libraries | They distribute copyrighted articles without permission. |

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

For each report that passes title and abstract screening and has no usable full text, Kittum tries
in order:

1. The local content store, by SHA-256, DOI, or PMID.
2. Europe PMC full-text XML and PDF.
3. PubMed Central's copy in the PMC Article Datasets.
4. Unpaywall and OpenAlex OA locations.
5. Publisher PDFs: the links a publisher deposits with Crossref where Crossref also lists a
   Creative Commons licence for that version.
6. medRxiv and bioRxiv, then arXiv, for a preprint's own DOI.
7. Semantic Scholar's `openAccessPdf`.
8. CORE, with a key.
9. The human upload: one PDF at a time, or many at once.

Enrichment finds the locations of steps 2, 4, and 5 for every record. Kittum asks the sources of
steps 3, 6, 7, and 8 about one report at a time, only when the waterfall reaches them, and records
each question as an attempt (`retrieval.source_asked`) with the files the source named.

Every PDF is checked against its record before it becomes the full text: the record's DOI on the
first three pages, or its title on the first two (at least 95 similar, ignoring case, spacing,
accents, punctuation, and hyphenation). A PDF that shows neither is a `wrong_document` attempt, is
not stored as a document, and the waterfall moves on. A PDF without text on its first pages, or a
record without a DOI or usable title, cannot be checked and is accepted.

Kittum records the source, the licence, and the acquisition time for every file, and every attempt,
failed ones included, in the ledger.

### When a source is down

A source that fails during enrichment or retrieval is recorded with its HTTP status and what it
said, and the workbench shows it in plain words, for example "Europe PMC did not answer (HTTP 503,
the service was down) for 24 records". **Retry failed sources** (Retrieval, key `r`, the methods
lead's alone) enriches again only the records a source failed for, then fetches the reports among
them that still have no full text, and fetches again the reports a source failed for during
retrieval (no answer, a 5xx, or a 429). A source that answered for another work is not retried.
Every retry and every attempt is a ledger event.

### Reports open access does not give

Retrieval exports the reports sought for retrieval without a usable full text as CSV or RIS: DOI,
PMID, title, authors, journal, year, the sources tried, why each attempt failed, and a file name to
give the PDF. Whoever gets the PDFs (a customer, a library) sends them back, and the lead uploads
them at once. Kittum matches each PDF by the DOI or PMID in its file name, or by the same check as
above, and holds the rest for the lead to assign or discard.

Licensed publisher TDM interfaces and institutional access (EZproxy, OpenAthens) are not built. The
proposal is issue #64.
