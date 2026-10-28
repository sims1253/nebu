# Dependencies

This page records which dependencies Kittum uses and why. For each one it looks at fit for Kittum,
quality, maintenance, and licence. Document tools and models have their own page,
[Document and model tools](document-tools.md), because their accuracy needs more room. This page
covers the licence decision and the application stack.

Facts were checked on 28.10.2026. "Releases" counts releases in the twelve months before that
date. "Commits" counts commits on the default branch in the 90 days before it. Both come from the
GitHub API. A count shows activity, not quality, so the verdicts also weigh the backing
organisation and open bugs.

## Licence

Kittum is licensed **AGPL-3.0-or-later**, as is the rest of the Nebu repository.

What that changes:

- A dependency's licence must be compatible with AGPL-3.0 for the way Kittum combines it. AGPL,
  GPL-3.0, LGPL, MPL-2.0, Apache-2.0, MIT, and BSD are; GPL-2.0-only and CDDL code cannot be
  linked into Kittum. A separate program that Kittum only calls (a database, an OCR binary) is
  judged on its own terms. PyMuPDF, Ghostscript, and psycopg (LGPL) are fine.
- The Janus reader swap is no longer needed. Janus keeps PyMuPDF, and Kittum uses Janus as it is.
- Anyone who uses Kittum over a network can get its source (AGPL section 13). The workbench footer
  links to the source of the running version.
- Clients who receive a handover bundle get the `verify` tool under the AGPL, with its source.
- Selling a proprietary licence later would need copyright in every contribution. Kittum has one
  author now. If outside contributions start, decide on a contributor agreement first.

What stays blocked, in code and in model weights:

- Non-commercial terms (CC-BY-NC, PolyForm Noncommercial).
- Revenue, funding, or user caps (MinerU code from 3.1.0, Datalab weights, Mistral Medium 3.5,
  Llama).
- Field-of-use limits and no-competition clauses (dots.ocr, Chandra, MedGemma).
- Source-available licences (BSL, SSPL, FCL).

These terms are "further restrictions" under AGPL section 10, so they are incompatible with the
licence and not only with the business. Weights get their own allowlist, because a weights licence
is not visible to an SPDX scanner. A CI job checks SPDX identifiers for code and the allowlist for
weights.

## Application stack

### Store and storage

| Dependency | Licence | Releases | Commits | Notes | Verdict |
| --- | --- | --- | --- | --- | --- |
| PostgreSQL 18 | PostgreSQL | 18.6 current; supported to November 2030 | | `uuidv7()` in the database, asynchronous I/O, OLD and NEW in `RETURNING`. Version 19 reached RC1 on 15.10.2026 with GA planned for 19.11.2026. | **Use 18.** Add 19 to CI and move after 19.2. |
| SQLite | Public domain | | | Handover bundle and test adapter | **Use** |
| MinIO | AGPL-3.0 | 0 | 0 | The console was stripped in 2025, binaries stopped in October 2025, and the repository was **archived on 25.04.2026** | **Remove** |
| Ceph RGW | LGPL | Active (Tentacle 20.2) | | Mature S3 Object Lock. Heaviest to run. | **Use** where WORM storage is required |
| SeaweedFS | Apache-2.0 | 49 | about 1,200 | Object Lock with retention and legal hold. Lock-enforcement fixes landed in October 2026. One lead maintainer. | Optional, after the conformance test |
| RustFS | Apache-2.0 | 61 | about 2,400 | 1.0 GA on 16.09.2026. A pre-GA bug could delete objects under COMPLIANCE retention. | Optional, after the conformance test |
| Garage | AGPL-3.0 | about 5 | | No object versioning, so no Object Lock | Development only |
| Filesystem with content addressing | | | | Hashes detect tampering but do not stop deletion | Development, tests, and the bundle |

Object storage sits behind a thin S3 interface. Before any store holds audit data, it must pass an
Object Lock conformance test: write under COMPLIANCE retention, try to delete, try to shorten the
retention, and set and lift a legal hold.

### Jobs

| Dependency | Licence | Releases | Fit | Verdict |
| --- | --- | --- | --- | --- |
| Own `SKIP LOCKED` lease table | | | A job row has `locked_until`, a heartbeat, attempts, and an idempotency key. Job state and its `audit_event` commit in the same transaction. | **Use** |
| procrastinate | MIT | 9 | A Postgres job table on psycopg 3 with retries and locks | Fallback if the own table grows |
| PgQueuer | MIT | 18 | Similar; one maintainer | Optional |
| DBOS Transact, Hatchet, Temporal | MIT | Very active | Each brings its own workflow vocabulary, and Hatchet and Temporal need a separate engine | **Avoid** |

### Python libraries

| Dependency | Licence | Releases | Commits | Notes | Verdict |
| --- | --- | --- | --- | --- | --- |
| Python 3.13 | PSF | | | `uuid.uuid7()` arrives only in 3.14, so IDs come from Postgres | **Use** |
| uv | Apache-2.0 or MIT | 100 | about 1,300 | | **Use** |
| FastAPI | MIT | 100 | about 350 | Same as Janus | **Use** |
| Pydantic 2.14 | MIT | 18 | about 230 | Contracts and model output schemas | **Use** |
| psycopg 3.3 | LGPL-3.0 | 8 on PyPI | about 95 | `class_row` with Pydantic models, pipeline mode | **Use** |
| dbmate | MIT | 16 | | Plain SQL migrations | **Use**. The SQLAlchemy ORM would get in the way of hash-chained writes. |
| scipy | BSD-3-Clause | 9 | | Clopper-Pearson through `binomtest(...).proportion_ci("exact")`; `hypergeom` for the stop test | **Use**. Capture-recapture and split-conformal thresholds are about 50 lines each, written in `stats`. |
| rapidfuzz | MIT | 6 | | Quote verification and dedupe | **Use** |
| rfc8785 (Trail of Bits) | Apache-2.0 | 0 (0.1.4, September 2024) | 7 | Small and finished. Pin it and test against the RFC vectors. | **Use** |
| rfc3161-client (Trail of Bits) | Apache-2.0 | 4 | 33 | | **Use** |
| cryptography | Apache-2.0 or BSD | 11 | | Ed25519 checkpoint signatures | **Use**. PyNaCl would duplicate it. |
| pyrate-limiter | MIT | 10 | | Its Postgres bucket shares one rate limit across all workers | **Use** in the source gateway |
| stamina | MIT | | | Retries with jitter and instrumentation, on tenacity | **Use** |
| httpx | BSD-3-Clause | 0 (0.28.1, December 2024) | | One maintainer; issues reportedly closed in February 2026 | **Use with care**. aiohttp or niquests if it decays further. |
| ruff | MIT | 49 | | | **Use** |
| ty | MIT | Pre-1.0 | | Janus uses it | Optional. Gate on pyright until 1.0. |

### Workbench

The workbench follows [t3code](https://github.com/pingdotgg/t3code), a large, fast-moving React
app (MIT, about 31,000 stars) whose stack was checked on 28.10.2026. Janus uses Bun and turbo;
moving it over is a separate change.

| Dependency | Licence | Releases | Commits | Role | Verdict |
| --- | --- | --- | --- | --- | --- |
| pnpm 12 and Node 26 | MIT | 100 | about 2,400 | Workspace and runtime | **Use** |
| Vite+ 1.4 (`vp`) | MIT | 100 | about 570 | One toolchain for Vite 8, Vitest, oxlint, and oxfmt, with cached task runs | **Use** |
| oxc (oxlint, oxfmt) | MIT | 100 | about 2,000 | Lint and format through Vite+ | **Use** |
| tsc-rs (`@tsc-rs/typescript`) | | | | Drop-in, faster typecheck, as t3code uses | **Use** |
| knip | ISC | 100 | about 340 | Unused files, exports, and dependencies | **Use** |
| React 19.3 | MIT | 31 | about 140 | | **Use** |
| TanStack Router | MIT | 100 | about 280 | Typed routes | **Use** |
| Effect 4 with Effect Schema and `@effect/atom-react` | MIT | 100 | about 1,900 | Contracts, typed client, and client state. `@effect/openapi-generator` builds the client from FastAPI's OpenAPI. | **Use** |
| Base UI 1.9 | MIT | 15 | about 450 | Accessible unstyled components, styled with Tailwind | **Use** |
| Tailwind 4.3 | MIT | 12 | 64 | | **Use** |
| Legend List 3.7 | MIT | 32 | about 220 | Virtualized lists for screening queues and page strips | **Use** |
| Vitest browser mode with Playwright | Apache-2.0 | 14 | about 690 | Component and end-to-end tests | **Use** |
| Third-party licence notices | | | | t3code generates them with a script (`licenses:sync`). Kittum needs the same for AGPL distribution. | **Use** |

The PDF viewer decides how fast a reviewer can check an anchor, so it got a closer look:

| Viewer | Licence | Releases | Fit for a 300-page CSR with anchored highlights | Verdict |
| --- | --- | --- | --- | --- |
| Own viewer, as in Janus | AGPL (Kittum) | | The server renders page images with PyMuPDF. The client virtualizes pages with Legend List and lays an invisible text layer from the reader's word boxes over each image, plus the highlight overlay. The browser never interprets the PDF, so what the reviewer selects is what the parser saw. | **Use** |
| pdfjs-dist 6.4 (Mozilla) | Apache-2.0 | 12 | Renders visible pages only. Kittum would still draw its own overlay. | Fallback if server rendering is too slow |
| EmbedPDF | MIT packages; its CloudPDF server is source-available | 55 | PDFium compiled to WASM in a worker, with tiling and virtual scrolling. The same engine as pypdfium2. | Spike against a real CSR |
| react-pdf (wojtekmaj) | MIT | 5 | One component per page; needs added virtualization | Optional |
| react-pdf-highlighter | MIT | 0 | No release since 2024 | Avoid |
| Nutrient (PSPDFKit), Apryse, Foxit | Proprietary | | | Blocked |

Anchors are page numbers and boxes in PDF points, the same frame the parsers produce. The viewer only
draws them. That keeps anchors independent of the viewer: pdf.js and PDFium split text into runs
differently, so a browser text layer would tie an anchor to one engine.

## Document tools and models in short

The full comparison is on [Document and model tools](document-tools.md).

| Job | Pick | Main reason |
| --- | --- | --- |
| Page triage | pdf-inspector (Firecrawl) | Deterministic, fast check of which pages need OCR and which have broken encodings |
| Text layer and rendering | PyMuPDF 2.0, with pypdfium2 as a cross-check | Fastest and most precise geometry; Janus uses it |
| Layout and tables | Docling | Maps table cells with spans back onto PDF text cells; top three on PubMed Central tables; 100 releases a year |
| Scans | OCRmyPDF with Tesseract, RapidOCR | A text layer keeps scans on the same anchoring path |
| Hard pages | PaddleOCR-VL-1.6 and Nemotron 3.5 Parse on GPU | Strong on OmniDocBench and ParseBench respectively; output re-anchored to the text layer |
| Scholarly metadata | GROBID | The standard for headers and references |
| Deterministic extraction | Janus | Typed decimals and locators |
| Agent extraction | PydanticAI with Kittum's quote verifier | LangExtract's offsets are not reliable enough for provenance |
| Judges | Qwen3.8-27B and Gemma-4-31B | Two separate lineages, the top two deployable models on the BRIDGE clinical board, one GPU each |
| Main extractor | Qwen3.8-27B | Best independent long-context score among models that fit (AA-LCR 0.82); 262k context |
| Long documents | Nemotron 3.5 Super VL | 1M context and page images for CSRs beyond 256k tokens |
| Serving | vLLM with xgrammar; Ollama on laptops | Most active serving stack; a batch-invariant mode to test |

## Open checks

- Ask Artifex where the corresponding source for PyMuPDF Layout is, before relying on Layout.
- Benchmark Qwen3.8-27B, Gemma 4, and Nemotron 3.5 Super VL on the gold data; gpt-oss-120b stays as a baseline.
- Test vLLM batch-invariant mode on each picked model.
- Run the Object Lock conformance test on the chosen store.
- Spike pdf.js and EmbedPDF on a 300-page CSR.
- Confirm the PubTator 3 terms and rate limit, which only third parties report.

## Sources

- GitHub API, PyPI, and npm registry metadata, queried 28.10.2026
- [PostgreSQL versioning policy](https://www.postgresql.org/support/versioning/),
  [PostgreSQL 19 RC1](https://www.postgresql.org/about/news/postgresql-19-rc-1-3409/)
- [minio/minio](https://github.com/minio/minio) (archived)
- [RustFS WORM documentation](https://docs.rustfs.com/features/worm),
  [Ceph releases](https://docs.ceph.com/en/latest/releases/)
- [EmbedPDF licensing](https://github.com/embedpdf/embed-pdf-viewer)
- [GNU AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)
