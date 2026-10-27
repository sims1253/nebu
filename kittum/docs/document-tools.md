# Document and model tools

This page compares the tools Kittum could use to read PDFs, run OCR, extract values, and run models,
and says which ones Kittum uses. Facts were checked on 28.10.2026 against GitHub, PyPI, Hugging
Face model cards, and licence files. Release counts cover the twelve months before that date.
"Vendor" marks a number reported by the tool's own authors. Recheck exact versions when pinning:
weights licences change more often than code licences.

Kittum is AGPL-3.0-or-later. The [licence policy](../DESIGN.md#licence-policy) allows any
open-source code licence, including AGPL and GPL. It blocks non-commercial terms, revenue or user
caps, field-of-use limits, and no-competition clauses, in code and in weights.
[Dependencies](dependencies.md) covers the rest of the stack.

## What Kittum needs from a parser

A parser is useful to Kittum only if its output can anchor evidence. That means:

- Text with character offsets, and geometry for every character or word in PDF coordinates.
- Tables with cells, row and column spans, header paths, and cell geometry.
- Reading order across columns, so that a quote prefix and suffix make sense.
- The same output for the same input, or at least output that a content hash can tell apart.

A tool that only produces Markdown can help a model read a document. It cannot ground evidence by
itself.

## PDF parsers and layout tools

| Tool | Licence | Fit | Evidence | Momentum | Verdict |
| --- | --- | --- | --- | --- | --- |
| PyMuPDF 2.0 | AGPL-3.0 or paid Artifex licence | Character quads, fast rendering, `find_tables` with cell boxes. Janus already uses it. 2.0 bundles pymupdf4llm and the Layout model. | Fastest text extraction in the py-pdf benchmark (0.1 s per document, quality 94%). Layout F1 0.864 (vendor). | 2.0 on 19.10.2026; 9 releases; Artifex | **Use** as the base text layer and renderer. |
| pypdfium2 (PDFium) | Apache-2.0 or BSD-3-Clause | Character boxes and rendering. No layout. The engine Chromium uses. | 0.1 s per document, quality 95%, the best in the py-pdf benchmark | 5.14.0 on 04.10.2026; 19 releases | **Use** as the second engine for differential tests. Docling uses it anyway. |
| pdfplumber and pdfminer.six | MIT | Inspectable characters, lines, and a ruled-table finder | 8.9 s and 5.6 s per document; pdfplumber quality 74% | pdfplumber 3 releases, no commits in 90 days; pdfminer.six slowing | Debugging only |
| Docling | MIT. Heron layout and Granite-Docling weights Apache-2.0. TableFormer weights Apache-2.0 or CDLA-Permissive-2.0. | Every item carries a box and a character span. Table cells carry spans and boxes and map back onto PDF text cells. | Heron-101 layout about 78% mAP. CPU 1.3 to 3.1 s per page, GPU 0.5 s per page on an L4 (paper). Granite-Docling 0.97 structure TEDS on FinTabNet (vendor). | 2.143.1 on 27.10.2026; 100 releases; IBM and LF AI and Data | **Use** for layout and table structure. Cell text comes from the vector text layer, never from OCR, when a text layer exists. |
| GROBID 0.9.1 | Apache-2.0 | TEI headers, authors, references, citation contexts, optional coordinates | Citation fields micro-F1 87.6 and header 78.1 on 1,943 PMC papers; 0.75 s per PDF (project benchmark) | 0.9.1 on 04.08.2026; 2 releases; steady | **Use** for metadata and references, as a Docker service |
| Janus | MIT | Deterministic locators for labelled tables and text, compound cell parsing, exact decimals, field statuses | Measured on the Janus examples | Sibling project | **Use** as the deterministic extraction path |
| Camelot 2.0 | MIT, Ghostscript backend AGPL | Cell grids for ruled tables; weak on three-rule clinical tables | No current benchmark found | 2.0.0 on 04.06.2026; 2 releases | Optional adapter |
| MinerU 4 | Code from 3.1.0: Apache-2.0 plus a licence requirement above 100M monthly users or USD 20M monthly revenue, mandatory attribution, and termination on breach. Code up to 3.0.9 is AGPL. MinerU2.5-Pro weights and `mineru-vl-utils` are plain Apache-2.0. | Span-level boxes, HTML tables | OmniDocBench v1.6 overall 95.75 for MinerU2.5-Pro, table TEDS 93.4 (benchmark run by the MinerU team) | 69 releases; very fast | **Exclude** the `mineru` package. The MinerU2.5-Pro weights through `mineru-vl-utils` are an optional GPU adapter. |
| marker, Surya, Chandra (Datalab) | Code Apache-2.0. Weights under a modified OpenRAIL-M: free only below USD 2M to 5M funding or revenue, Chandra adds a no-competition clause, and the Surya layout weights are CC-BY-NC-SA. | Block boxes and a JSON tree | olmOCR-Bench 76.0 (marker) and 85.8 (Chandra 2), both vendor | Active | **Exclude.** Benchmark only. |
| Nougat | MIT code, CC-BY-NC weights | Markdown only, no coordinates | | No release since 2023 | **Exclude** |
| Unstructured | Apache-2.0 open core | Element boxes; mostly wraps other tools | Trails Docling on scientific tables | 61 releases | **Skip.** Redundant. |

## OCR for scans

Most journal PDFs have a text layer. Old comparator trials and some CSR appendices do not. Kittum
prefers OCR that writes a text layer back into the PDF, because then a scan goes through the same
anchoring path as a born-digital file.

| Tool | Licence | Fit | Evidence | Momentum | Verdict |
| --- | --- | --- | --- | --- | --- |
| OCRmyPDF 17 with Tesseract 5.5 | MPL-2.0 and Apache-2.0; Ghostscript AGPL | Adds a searchable text layer with word boxes. CPU, deterministic. | Weak on tables and formulas | OCRmyPDF 22 releases | **Use** as the baseline for scans |
| RapidOCR 3.10 (ONNX port of PP-OCR) | Apache-2.0 code and weights | Line boxes, word boxes optional; built into Docling; CPU | No current independent benchmark | 14 releases | **Use** inside Docling and where Tesseract is weak |
| PaddleOCR-VL-1.6 and PP-StructureV3 | Apache-2.0 code and weights | Block boxes and HTML tables; StructureV3 gives table cell boxes | OmniDocBench v1.6 overall 96.34, table TEDS 94.8 (vendor) | 8 releases; Baidu | Optional GPU adapter for hard scanned tables |
| GLM-OCR 0.9B | Apache-2.0 code, MIT weights | Block layout | OmniDocBench v1.6 overall 95.22 | 5 releases | Benchmark, possible adapter |
| olmOCR 2 | Apache-2.0 code and weights | Markdown per page, no word boxes | olmOCR-Bench 82.4 (own benchmark) | No commits in 90 days | Benchmark only |
| DeepSeek-OCR 2 | MIT and Apache-2.0 | Block boxes in grounding mode | OmniDocBench v1.6 overall 90.25 | No commits since February 2026 | Benchmark only |
| dots.ocr | MIT plus a use policy that bans "unauthorized digitization of publications" | Block boxes | OmniDocBench v1.6 overall 90.77 | Quiet since March 2026 | **Exclude.** Field-of-use limit. |
| Mistral OCR 4 | Proprietary API, EU-hosted | Boxes and confidence scores (press reports) | Older version: OmniDocBench v1.6 overall 85.66 | Commercial service | Opt-in for public papers only. CSRs never go to it without client sign-off. |

Two cautions about these numbers. The OmniDocBench maintainers are the MinerU team, and model
authors submit most entries. And a vision model gives block-level boxes at best and is not bitwise
deterministic, so Kittum treats its output as a hypothesis and re-anchors every value to the text
layer.

## Extraction libraries

| Tool | Licence | Fit | Momentum | Verdict |
| --- | --- | --- | --- | --- |
| LangExtract (Google) | Apache-2.0 | Few-shot LLM extraction that aligns each value to a character interval | 1.7.1 on 08.10.2026; 10 releases. "Not an officially supported Google product." Open alignment bugs: #289 (wrong `char_interval` on long extractions, open since December 2025), #591 (fuzzy alignment skipped on shared-prefix matches), #450 (IndexError in fuzzy alignment). | **Optional**, for chunking or the HTML viewer. It cannot be the source of provenance while its offsets are unreliable. |
| PydanticAI | MIT | Typed, model-agnostic agent loop with tool calls and structured output | 2.62.0; 100 releases a year, so pin it | **Use** for the bounded extraction agent |
| Instructor | MIT | Pydantic-validated output with retries | 13 releases; stable | Optional. PydanticAI covers it. |
| LangChain and LangGraph | MIT | General agent frameworks | | **Skip.** They would add a shallow layer over `judge` and `extraction`. |

Kittum takes quotes from schema-constrained model output and verifies them with its own
deterministic matcher. That matcher, not any library, decides whether a value is grounded.

## The parser seam

`documents` has one external seam, `Parser`, with real adapters from the start:

```python
class Parser(Protocol):
	recipe: ParserRecipe  # engine, version, container digest, weights, OCR languages, settings

	def parse(self, pdf: bytes) -> RawLayer: ...
```

`documents.derive(document, recipe)` runs the parser, builds canonical text, the normalization
map, page geometry in unrotated PDF points, and the table model, and stores them as an immutable
layer. Callers never see engine coordinates or engine types. The PoC ships three adapters: PyMuPDF,
pypdfium2, and Docling. GROBID is a fourth adapter for metadata and references.

Janus plugs in on its own side. Kittum converts a layer into Janus's `DocumentEvidence` contract and
calls `janus.extraction.extract`.

### PyMuPDF Layout

PyMuPDF 2.0 ships its Layout model inside the AGPL wheel. Layout releases up to 1.28.0 were
PolyForm Noncommercial; 1.28.2 (06.08.2026) was the first AGPL release. The Layout source is not in
the public PyMuPDF repository. Until Artifex says where the corresponding source is, Kittum uses
PyMuPDF for text, geometry, and rendering, and takes layout from Docling. Pin `pymupdf>=2.0` so no
non-commercial Layout build can enter the lockfile.

## Models

Kittum runs open-weight models locally for the PoC and for confidential documents. Hosted models
are an option for public literature. Confirm the picks with a one-day benchmark on the gold data at
the start of the spike.

| Stage | Pick | Why | Weights licence |
| --- | --- | --- | --- |
| Title and abstract judge, family 1 | gpt-oss-120b (gpt-oss-20b on a laptop) | One of the two families in the largest published open-weight screening test (below). On vLLM's batch-invariance list. | Apache-2.0 |
| Title and abstract judge, family 2 | Gemma-4-31B-it (Gemma-4-26B-A4B or 12B on a laptop) | The other family in that test; 256k context | Apache-2.0 since Gemma 4. Earlier Gemma versions and EmbeddingGemma v1 used the Gemma terms. |
| Judge family 3 (experiment) | Qwen3.8-27B; Granite 4.2-30B as backup | Keep only if it finds relevant records the other two miss | Apache-2.0 |
| Full-text screening and prose extraction | Qwen3.8-27B (262k context); Mistral Small 4 as the alternative | Long context. No independent faithfulness benchmark exists for either, so Kittum's quote verifier carries the guarantee. | Apache-2.0 |
| Search strategy drafting and PRESS review | The strongest local reasoning model on the box, or a frontier model for public topics | The specialist edits and approves. The model never runs a search unreviewed. | |
| Embeddings for queue order and clustering | MedCPT (PubMed-trained, English) and Qwen3-Embedding (multilingual) | Never used for primary search | Public domain; Apache-2.0 |
| Reranker (later, large topics only) | MedCPT cross-encoder or Qwen3-Reranker | Orders the queue only | Public domain; Apache-2.0 |

The screening evidence for the first two rows: Fagerberg et al. (medRxiv, 2026) ran LLM pairs over
419 Cochrane reviews and 26,892 records. The open pair Gemma-4-31B plus gpt-oss-120b reached 95.9%
sensitivity (95% CI 94.7 to 97.0) under an OR rule, against 98.0% for the best closed pair. Other
2026 work points the same way. A compact Llama-3.1-8B reached 99.7% sensitivity but only 12.7%
specificity. Spelling out inclusion criteria and exclusion reasons raised F2 by about 29% across 22
open models (SciLitBench).

Excluded models:

| Model | Reason |
| --- | --- |
| Llama family | Custom licence with an acceptable-use policy and a user cap |
| Mistral Medium 3.5 | Modified MIT that withdraws rights above USD 20M monthly revenue |
| Qwen3.8-Flash-Next | Qwen Community License: a separate licence for "Model as a Service" businesses |
| MedGemma | Health AI Developer Foundations terms with a prohibited-use policy and remote restriction rights |
| Kimi K3, GLM-5.3 | Custom terms, and too large for one GPU box |

### Serving

| Component | Licence | Momentum | Verdict |
| --- | --- | --- | --- |
| vLLM 0.32 | Apache-2.0 | 33 releases; about 4,600 commits in 90 days | **Use** on the GPU box |
| xgrammar | Apache-2.0 | 20 releases | **Use** as vLLM's structured-output backend; llguidance (MIT) as fallback |
| Ollama 0.40 on llama.cpp | MIT | 100 releases | **Use** on laptops |
| SGLang | Apache-2.0 | 33 releases | Optional. Deterministic mode costs 25 to 45% throughput (vendor). |
| Outlines | Apache-2.0 | 9 releases; slowing | Skip. xgrammar covers it. |

### Structured output

Every model call uses JSON Schema-constrained decoding: vLLM structured outputs (xgrammar or
llguidance), or Ollama's schema format. Kittum generates the schema from the Pydantic models.
Decisions have no free-text fields. Rationale is a short string beside a coded status.

After decoding, a deterministic verifier checks every quote against the source text. It allows only
whitespace and hyphenation normalization. A failed check downgrades the criterion to `unclear` and
flags it.

### Determinism

Temperature 0 and a fixed seed do not make vLLM bitwise deterministic. Batch shape changes the
order of floating-point reductions. vLLM has a batch-invariant mode (`VLLM_BATCH_INVARIANT=1`), but
its docs call it beta. It is validated for gpt-oss and Qwen3 but not for Gemma 4 or the Qwen3.5 and
later hybrids, and it had silent-correctness bugs as recently as October 2026 (#59940, #58899).
Kittum therefore:

- Stores every rendered prompt, raw completion, token log-probabilities where available, weights
  hash, and container digest.
- Replays from stored outputs for audit.
- Measures run-to-run agreement over three runs on a fixed sample, and reports it.
- Tries batch-invariant mode per model for spot re-execution, without promising identical bytes.

### Hosted models and confidentiality

A hosted model needs EU processing, a data processing agreement, zero retention, and no training on
inputs. Record the model identity the provider returns, not only the alias in the request. Client
CSRs stay on local models unless the client signs off in writing.

## Sources

- [py-pdf benchmarks](https://github.com/py-pdf/benchmarks)
- [OmniDocBench](https://github.com/opendatalab/OmniDocBench) and
  [olmOCR-Bench](https://github.com/allenai/olmocr/tree/main/olmocr/bench)
- [Docling technical report](https://arxiv.org/abs/2408.09869),
  [Docling 2025 paper](https://arxiv.org/abs/2501.17887), [Heron](https://arxiv.org/abs/2509.11720)
- [GROBID PMC benchmark](https://github.com/grobidOrg/grobid/blob/master/doc/benchmarks/Benchmarking-pmc.md)
- [MinerU licence](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md),
  [mineru-vl-utils](https://github.com/opendatalab/mineru-vl-utils)
- [PyMuPDF 2.0](https://pypi.org/project/pymupdf/2.0/),
  [pymupdf-layout 1.28.2](https://pypi.org/project/pymupdf-layout/1.28.2/)
- [PaddleOCR-VL-1.6](https://arxiv.org/abs/2606.03264),
  [dots.ocr](https://github.com/studio-dots-ai/dots.ocr), [Chandra](https://github.com/datalab-to/chandra)
- [LangExtract issues](https://github.com/google/langextract/issues)
- [Fagerberg et al. 2026](https://www.medrxiv.org/content/10.64898/2026.06.02.26354724v1.full),
  [SciLitBench](https://arxiv.org/html/2609.05505v1),
  [compact LLMs for screening](https://www.cambridge.org/core/journals/research-synthesis-methods/article/compact-large-language-models-for-title-and-abstract-screening-in-systematic-reviews-an-assessment-of-feasibility-accuracy-and-workload-reduction/CB00FD70434780029EF6C027055331BA)
- [vLLM batch invariance](https://docs.vllm.ai/en/latest/features/batch_invariance/),
  [SGLang deterministic inference](https://lmsys.org/blog/2025-09-22-sglang-deterministic/)
- Model cards: [gpt-oss-120b](https://huggingface.co/openai/gpt-oss-120b),
  [Gemma-4-31B-it](https://huggingface.co/google/gemma-4-31B-it),
  [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B),
  [Mistral Small 4](https://huggingface.co/mistralai/Mistral-Small-4-119B-2603),
  [MedCPT licence](https://huggingface.co/ncbi/MedCPT-Query-Encoder/blob/main/LICENSE),
  [Qwen3-Embedding](https://huggingface.co/Qwen/Qwen3-Embedding-8B)
