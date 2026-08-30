---
name: schema-authoring
description: Use when a user needs specific values pulled out of a PDF and checked against expected data — interview the user, write the Janus comparison specification, and verify it end to end.
---

# Comparison-specification authoring

Turn "I need these numbers from this PDF, checked against my records" into a `specification.json`
that compiles, extracts, and matches. Self-contained for any coding agent.

Grammar of record (paths relative to the directory containing `apps/server`):
`docs/comparison-specification-v1.md` and
`packages/contracts/schema/comparison-specification.v1.json`. The schema rejects unknown keys at
every level, so typos fail loudly with a JSON path.

## The interview

Run these rounds before writing any JSON. Discover document facts yourself; never guess what only
the user knows.

1. **What information?** Which values matter, which tables or boxes they live in, and what the
   downstream consumer expects — types, date formats, rounding latitude, what happens on a
   mismatch. Capture the exact wording of any row or column labels the user quotes.
2. **Against what?** Where the expected values live: an existing dataset (JSON, CSV, XLSX), values
   they transcribe, or nothing (extraction only — see "Stub reference"). Never invent ground truth.
3. **How close is close enough?** Tolerances and severity (`error` is the default), and whether
   formatting differences should pass. If the user shrugs, propose `numeric` +
   `numeric_punctuation` + a small absolute tolerance and confirm.
4. **Read the document yourself.** `pdftotext -layout <pdf> -` shows the real row labels, column
   headers, and cell formats. Write aliases from what is printed, not what a clean layout "should"
   say: PDF text wraps and reorders, so trust the tool output over the visual layout. When
   `pdftotext -layout` and the extractor's raw cell text disagree, trust the extractor (validated
   in the field): its cells come from the document's own geometry, while a text dump reflows
   wrapped and columnar text.
5. **Draft, verify, iterate.** Then report one line per field (status plus extracted value),
   mismatches first — not just the file.

### No user available

A batch run has nobody to ask, so the rounds above collapse into documented defaults instead of
improvised guesses. Compare with `numeric` + `numeric_punctuation` and an `absolute_tolerance` of
half the last printed decimal place (`0.34` → `0.005`, `12.5` → `0.05`). Never invent reference
values — transcribe them from the document and say so in the report. Record every guess (defaults
applied, tolerance chosen, values transcribed) in a comment or the run's notes, so a later human
can review and correct each one.

## Grammar cheatsheet

Minimal valid field, in context:

```json
{
  "schema_version": "1",
  "name": "Billing check",
  "sections": [
    {
      "key": "billing",
      "label": "Billing",
      "fields": [
        {
          "key": "grand_total",
          "label": "Grand total",
          "reference": { "pointer": "/totals/grand_total" },
          "value": { "type": "decimal" },
          "locate": {
            "strategy": "table_label",
            "labels": ["Grand total", "Total due"]
          },
          "compare": {
            "operator": "numeric",
            "normalizers": ["numeric_punctuation"],
            "absolute_tolerance": 0.01
          }
        }
      ]
    }
  ]
}
```

Decisions, not definitions — the docs cover the rest.

Five situations recur in every document; settle them before writing fields.

1. A cell holding several numbers with structure (`123 (56.7%)`, `0.26 [0.22, 0.31]`) needs
   `source_shape` + `component`.
2. The same row label under different group headers needs `section_labels`.
3. The same label in several columns needs `column_labels`.
4. Many rows sharing one shape (line items, period rows, per-jurisdiction rows) need a `rows`
   generator — one field with `rows.label_pattern` capturing the row key, the reference pointing at
   an object keyed by the sanitized row keys — not one copy-pasted field per row. Per matched label
   only the best-scoring value column generates a row, so `column_labels` selects the column (a
   column tie inside the 0.03 gap makes the row `ambiguous`, not guessed). Patterns run on cell
   text, where `.` does not cross newlines — use `[\s\S]` to capture across a wrapped label
   (`"…, Precinct\n1"`), or the key stops at the wrap. A `locate.value_pattern` gates rows BEFORE
   component parsing: a candidate whose value breaks the shape is not a row at all (no key, no
   entry); this also keeps foreign tables' rows out when their values don't fit the shape.
5. Amounts with currency symbols need the `currency_symbol` normalizer before
   `numeric_punctuation`.

- **value.type** records what the value is (`text`, `integer`, `decimal`, `percentage`, `date`,
  `boolean`); comparison behavior comes entirely from `compare.operator`. `percentage` does not
  strip `%` — add `percent_symbol`.
- **Compound cells**: when one cell holds a number plus its uncertainty (`12 (34%)`, `5.2 (0.8)`,
  `10-20`, `0.26 [0.22, 0.31]`), set `source_shape` and `component` together (both or neither) and
  declare one field per number: `count_percentage` → `count` | `percentage`;
  `mean_standard_deviation` → `mean` | `standard_deviation`; `range` → `minimum` | `maximum`;
  `estimate_interval` → `estimate` | `standard_error` | `lower` | `upper` (`standard_error` when
  the cell prints the error before the interval, as in `0.261 (0.025) [0.213, 0.310]`). A cell
  that doesn't match the declared shape is `not_extracted` with an explanation. Trailing
  annotations (`; P<0.001; BF>100`) and a space after the decimal point (`0. 261`) are tolerated —
  the extractor strips them — but never let a plain type carry the whole cell: a `decimal` field
  hands `"0.34 [0.29, 0.38]; P<0.001; BF>100"` straight to `numeric`.
  `source_shape` picks the one number out.
- **operator**: `exact` (text equality after normalizers), `numeric` (both sides must parse as
  numbers), `date` (each side parses independently as ISO `YYYY-MM-DD` or dotted day-first
  `02.09.2026`; slashed month/day forms are unsupported — they are ambiguous without a locale),
  `boolean` (`true/yes/1`, `false/no/0`). Default to `numeric` for anything the user calls an
  amount or a count; use `exact` only when the text itself is the fact (IDs, names, codes).
- **tolerances** (`numeric` only): pass if equal, or `|diff| <=` `absolute_tolerance`, or
  `|diff| / |reference| <=` `relative_tolerance`; against a reference of 0, the relative rule
  passes only on exact equality. Amounts compared after rounding: `absolute_tolerance: 0.01`.
- **normalizers** apply in order, to both sides, each at most once: `trim`,
  `whitespace_collapse`, `casefold`, `percent_symbol` (removes `%`, trims), `currency_symbol`
  (strips a leading `$`, `€`, `£`, or `¥`), `numeric_punctuation`. The last strips
  spaces, then canonicalizes separators: when both `.` and `,` appear, the later one is the decimal
  point and the other is dropped (`1.234,56` and `1,234.56` both become `1234.56`); a separator
  that repeats is thousands and is dropped; a lone separator is read as a decimal point, so
  `1,234` becomes `1.234`. Bare `1,234` is therefore ambiguous US-thousands vs European-decimals,
  and the reading picks decimals. Apply `numeric_punctuation` to BOTH sides so the convention
  cancels out, and don't give the reference a different lone-separator format than the document.
- **Leading currency symbol**: put `currency_symbol` ahead of `numeric_punctuation` and keep
  `numeric`. Without it `$1,234.56` fails `numeric` ("not numeric after normalization") — the
  operator has no other way past a leading symbol. Don't mirror the printed formatting into the
  reference and fall back to `exact`: that also pins thousands separators the operator was meant
  to absorb.
- **pointer forms** — references are normalized before pointers resolve. JSON keeps its tree
  (`/order/id`, `/rows/0/total`); CSV becomes `{"rows": [...]}` keyed by the header row
  (`/rows/0/value` — row order is positional, so read the CSV to learn which row is which). A CSV
  with exactly two columns (any header names) also gains a keyed view:
  `/lookup/<first-column-value>/value` binds fields by identity and later duplicate keys win, so
  it survives the export reordering rows; prefer it whenever row order varies. XLSX
  becomes `{"sheets": {"Sheet1": {"rows": [...]}}}` (`/sheets/Sheet1/rows/0/Amount`). CSV and
  XLSX need a first row of non-empty, unique headers; CSV values stay strings (`date` and
  `boolean` still parse them). `required` defaults to `true` — a missing pointer fails compilation
  with `REFERENCE_POINTER_NOT_FOUND`; `required: false` allows absence.
- Three locator strategies: `table_label` for table rows, `text_label` for label:value text lines, `text_span` for whole prose sections between anchors.
  `labels` fuzzy-match the row label (or the anchor line) — token-set similarity, case-insensitive — so list every printed
  spelling, including wrapped-line prefixes. `column_labels` fuzzy-match the value column's header cell;
  `section_labels` fuzzy-match the row's section context (the group name the reader stamps on split
  grouped rows and indented rebuilt rows), and a miss costs up to 30 percent of the label score.
  `minimum_confidence` (0–1, default `0.6`) is the floor; two candidates at different cells scoring
  within 0.03 of each other end `ambiguous` rather than guessed, and the explanation names the rival
  (its label and page/row/column), not just the tied scores.

- Value printed next to its label in running text ("Version: 8.0", "Melting point: 14 °C")? That is `locate.strategy: text_label` — same labels/aliases, same section_labels idea (nearest heading), no column_labels. Ruled tables stay `table_label`.
- Prose, not label:value — the wanted "value" is a whole section or paragraph? That is `locate.strategy: text_span`: `labels` anchor on the section heading, `end_labels` (e.g. `["References"]`) stops before the closing heading (or the span runs to the next same-or-higher heading), lines join across pages, and repeated running heads are ignored. Lines inside a ruled table stay out of the prose by default (`exclude_tables`, default true) — anchor on a table caption and you get the caption plus the paragraph after the table, not the table's numbers; `exclude_tables: false` keeps table lines in. Borderless-inferred "tables" (often prose misread as a grid) are never excluded, and the explanation says so. Not for single labeled values — those stay `text_label`/`table_label`.
- **Subset-rival anchor ties** (from a cold end-user run): a SHORTER line whose tokens all sit inside your alias ties it at 1.0 — the wrapped column header printing `Computational` / `Reproducibility` ties the alias `2. Computational Reproducibility`, and `section_labels` penalize both equally. The token-set tiebreak resolves this when the true anchor line carries every alias token and the rival carries only a subset — quote the printed heading. When the tiebreak cannot separate them (both or neither carry the full token set), the field stays `ambiguous` with the rival named in the explanation. Then alter the alias's TOKENIZATION — glue tokens (`Reproducibility-Check`), reorder them, or drop to a distinctive one — rather than lengthening it: a longer alias keeps the rival's tokens inside it and the tie at 1.0.
- `text_label` boundaries (learned the hard way in a cold end-user run): the value is only the
  FIRST text line of the pair; a wrapped value truncates. A value printed on the line BELOW its
  label does not pair at all — it reads as a tie and goes ambiguous. Never lower
  `minimum_confidence` to force a match: with a short alias it will confidently return a
  same-baseline value from an unrelated page. Quote the exact printed label and raise aliases
  instead. Inline `label: value` pairs need the colon.

## Pitfall playbook

Distilled from documents that broke naive specifications.

- **Repeated row labels → `section_labels`, never an invented label**: "Revolving" can sit under
  three different parents. Disambiguate with `section_labels` — grouped rows are split into
  sub-rows and stamped with their group name automatically, and indented rows in rebuilt
  borderless tables carry their parent as section context. Do not concatenate label parts into an
  alias the document never prints ("Total outstanding Revolving"). The row cell still says
  "Revolving", so that alias matches every "Revolving" row equally well; the candidates tie and
  the field ends `ambiguous`. The group name lives in section context, not in the row label. On
  rows with no section stamp, a `section_labels` entry scales the score down 30 percent, so only
  add one when the label genuinely repeats.
- **Same label in many columns**: a row repeated across result columns needs `column_labels`
  naming the column header, or the columns tie and the field is `ambiguous`. Near-identical
  headers ("Check I [4,000 iterations]" vs "Check II [5,000 iterations]") resolve when the alias is
  a complete token set of one header, not the other.
- **Header footnote markers**: releases glue markers to period headers — `Junp` (preliminary),
  `2025r` (revised). Quote the printed form in the alias; the fuzzy scorer tolerates close
  variants, so `Jun` still scores well against `Junp` — but the printed spelling is the safe one.
- **The first row can be data**: headerless tables (invoices, statements, form summaries) put real
  values in row 0, and the locator treats row 0 as a candidate. Don't assume a header exists;
  don't skip the first row when collecting labels.
- **Compound statistics cells** map to `source_shape` (above): one field per component; the point
  estimate is not interchangeable with an interval bound. Pick the shape by what the cell LOOKS
  like, never by what the number MEANS: a cell printed `0.261 (0.024)` — estimate with error, no
  interval — is `mean_standard_deviation`, even when the number is not a mean and you only want
  the estimate. `estimate_interval` requires brackets or a two-number parenthetical; the
  semantically-honest name yields `not_extracted` ("Value did not match the declared ...
  source shape"). Field keys and reference pointers stay semantic. Never hand the raw cell text back —
  `numeric` cannot parse `"0.34 [0.29, 0.38]; P<0.001; BF>100"`; `source_shape` picks the number
  out of it. Mirroring the whole cell into the reference with `exact` is the same mistake in
  disguise: it can score as a match while the downstream consumer still has to dig the number
  out. That defeats structured extraction.
- **Tables split across pages** keep column context — the reader carries the previous page's
  header over the continuation. One field per value, never per page.
- **Borderless statistical tables** defeat the ruled-table detector; the reader falls back to
  whitespace alignment and a word-geometry rebuild. Rebuilt cells carry no bounding boxes
  (`cell_bbox` is null) — comparison works; box-based highlighting does not.
- **Raster-image labels are unreachable**: the reader uses the text layer only, no OCR, so labels
  that exist purely as pixels cannot match. Aligned label/value text spans are rebuilt as a
  two-column table, so boxes whose labels survive as text still extract.
- **Printed dates**: the `date` operator parses each side independently as ISO `YYYY-MM-DD` or
  dotted day-first (`02.09.2026`), so a dotted European date now compares directly with no
  normalizers. Slashed month/day forms (`09/15/2023`) stay unsupported on purpose — without a
  locale they read two ways; use `exact` + `trim` with the reference stored as printed.

## Stub reference (extraction only)

When there is no reference dataset and the user only wants values out:

- Point every field at a pointer a minimal reference does not contain, with `required: false` —
  for example reference `{"fields": {}}` and pointers `/fields/grand_total`.
- Consequence: compilation and extraction run normally, but every comparison is `not_compared`
  ("Optional reference value is absent; comparison was skipped"). Extraction still yields raw and
  parsed values with locations.
- Alternative when the document itself is ground truth (an analyst capturing a release): build a
  real reference by reading values off the document, and comparisons run for real.

## Worked example

User says: "Each month we reconcile printed invoices against the ledger export. From the invoice
PDF I need the invoice number, invoice date, due date, net total, VAT rate, grand total, and the
paid flag. The ledger exports a CSV with a `field,value` header; the rows correspond to the bill's
fields." No tolerance opinions — amounts should survive rounding. Reading the PDF shows a
label/value block whose first row is data, ISO dates, amounts with thousands separators, `19%`,
and `false`. Decisions:

- CSV reference → it has exactly two columns, so use the keyed view: `/lookup/invoice_number/value`,
  `/lookup/grand_total/value`, and so on. Field identity, not row position, binds each pointer, so
  the ledger export can reorder rows. (`/rows/N/value` also resolves; the shipped example uses
  that form.)
- Two sections mirroring the document: header facts (number, both dates) and money summary
  (amounts, rate, flag).
- Invoice number: `text`, `exact` + `trim` (printed and stored identically).
- Dates: `date` operator, ISO on both sides.
- Net and grand totals: `decimal`, `numeric` + `numeric_punctuation` + `absolute_tolerance: 0.01`
  (printed with separators, reference without). If the invoice printed `$1,840.00`, add
  `currency_symbol` ahead of `numeric_punctuation`.
- VAT rate: `percentage`, `numeric` + `percent_symbol` (printed `19%`).
- Paid: `boolean`. Labels are exactly the printed row texts; `minimum_confidence` stays at the
  default.

The shipped `examples/invoice/specification.json` follows these decisions except that its pointers
use the positional `/rows/N/value` form — compare against it only after your own draft runs.

## Verification

Always run the pipeline; never ship an unverified specification. From the repository root, adapt
the three paths and run:

```bash
cd apps/server && uv run --no-sync python - <<'PY'
from pathlib import Path
from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.engine import ComparisonEngine
from janus.comparison.reference import ReferenceSource

doc = Path("../../examples/invoice/document.pdf")      # the PDF
spec = Path("/tmp/specification.json")                 # your draft
ref = Path("../../examples/invoice/reference.csv")     # reference data
plan = SpecificationCompiler().compile(
    spec.read_bytes(), ReferenceSource(ref.read_bytes(), ref.name))
evidence = TextLayerDocumentReader().read(doc, "document")
extraction = SchemaExtractor().extract(plan, evidence)
for c in ComparisonEngine().compare(plan, extraction):
    print(c.field_key, c.extraction_status.value, c.status.value,
          repr(c.document_value), repr(c.reference_value))
PY
```

`ReferenceSource(bytes, filename)` picks the parser from the filename suffix (`.json`, `.csv`,
`.xlsx`). Compile failures raise a diagnostics list (code, specification path, remediation hint) —
fix those first. Before tuning any threshold, READ THE SCORE BREAKDOWN in the field's extraction
explanation — every outcome prints `score: label x section x column x confidence = final, floor`
(and the runner-up's score on ambiguous/not_extracted) — and tune against those numbers, never by
bisection. Iterate on outcomes:

- `not_extracted`: the explanation carries the best candidate's score against the floor — read it
  first; then print the actual cell text (`pdftotext -layout`), fix aliases, or lower
  `minimum_confidence` only as far as the breakdown justifies.
- `ambiguous`: two candidates tie — the explanation carries both scores and names the rival (its
  text and location); add `column_labels` or `section_labels`, or a more specific alias. For a
  text_span anchor tied by a subset rival, read the subset-rival bullet above before lengthening
  the alias.
- `not_compared`: pointer missing (check the normalized reference shape, the row order, and the
  lookup key spelling — keys are first-column values, exactly as printed) or deliberate stub mode.
- `mismatch`: formatting — add or reorder normalizers; revisit tolerance with the user rather than
  widening it silently.
