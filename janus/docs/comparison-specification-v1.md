# Comparison specification v1

A comparison specification is a JSON object that names every value to check: where the expected value lives in the reference data, where the printed value lives in the PDF, and how to compare the two. The server validates every upload against the JSON schema `packages/contracts/schema/comparison-specification.v1.json` before it compiles. The schema rejects unknown keys at every level, so a typo fails with a path instead of passing silently.

## Example

This is `examples/purchase-order/specification.json`, complete:

```json
{
  "$schema": "https://janus.example/schemas/comparison-specification.v1.json",
  "schema_version": "1",
  "name": "Purchase order check",
  "sections": [
    {
      "key": "summary",
      "label": "Summary",
      "fields": [
        {
          "key": "order_number",
          "label": "Order number",
          "reference": { "pointer": "/order/id" },
          "value": { "type": "text" },
          "locate": {
            "strategy": "table_label",
            "labels": ["Order number", "Order no."]
          },
          "compare": {
            "operator": "exact",
            "normalizers": ["trim", "casefold"]
          }
        },
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

The document prints `PO-1042` for the order number and `1,275.50` for the grand total. The reference holds `"PO-1042"` and `1275.5`.

The order number matches under `exact` with `trim` and `casefold`. For the grand total, `numeric_punctuation` turns `1,275.50` into `1275.50`. The `numeric` operator reads both sides as numbers; they are equal. The `0.01` tolerance also permits differences up to `0.01`.

## Authoring decision rules

Five situations decide most of a specification. Check them before writing fields.

- A cell that holds several numbers with structure — `123 (56.7)`, `0.26 [0.22, 0.31]` — needs `value.source_shape` plus `value.component` to pick one number. A plain type hands the whole cell text to the operator.
- The same row label under different group headers needs `locate.section_labels`. The reader stamps the group name itself; the labels name which group's row you want.
- The same label in several columns needs `locate.column_labels` naming the column header. Without it the columns tie and the field is `ambiguous`.
- Many rows sharing one shape — line items, period rows, per-jurisdiction rows — need one `rows` generator field, not one field per row. See [Repeated rows](#repeated-rows-rows).
- An amount printed with a currency symbol needs the `currency_symbol` normalizer, listed before `numeric_punctuation`. A leading `$`, `€`, `£`, or `¥` otherwise breaks the `numeric` operator.

## Top level

- `schema_version`: must be `"1"`.
- `name`: specification name, 1–200 characters.
- `sections`: one or more sections.
- `$schema`: optional schema identifier.

Each section has:

- `key`: matches `^[A-Za-z0-9][A-Za-z0-9_.-]*$`, at most 128 characters.
- `label`: 1–256 characters.
- `fields`: one or more fields.

Section keys must be unique across the specification. Field keys must be unique within their section. The compiler rejects duplicates with `INVALID_SPECIFICATION`.

## Fields

A field under `sections[].fields` has `key` (same pattern and limit as a section key), `label` (1–256 characters), and four objects: `reference`, `value`, `locate`, `compare`. It may declare a `rows` generator, which replaces the single static definition with one extraction per matching row (see [Repeated rows](#repeated-rows-rows)).

### `reference`

- `pointer`: an RFC 6901 JSON Pointer into the normalized reference data, at most 2048 characters. Examples: `/order/id`, `/rows/0/total`, `/lookup/Invoice number/value`, `/sheets/Orders/rows/0/id`. The schema rejects pointers that do not conform.
- `required`: defaults to `true`. A missing pointer on a required field fails compilation with `REFERENCE_POINTER_NOT_FOUND`. With `required: false`, a missing pointer is allowed; the field is compared as `not_compared`.

The reference data is normalized before pointers resolve:

- JSON keeps its tree.
- CSV becomes `{"rows": [...]}`, one object per record. A CSV with exactly two columns (any header names) also gains a keyed view: `lookup` maps each first-column value to `{"value": ...}`, so the pointer `/lookup/<first-column-value>/value` binds a field by identity instead of row position. Later duplicate keys win. Prefer it over positional `/rows/N/value` for document families whose reference export reorders rows.
- XLSX becomes `{"sheets": {"Sheet1": {"rows": [...]}}}`.

CSV and XLSX need a first row of non-empty, unique column headers. The server rejects an XLSX workbook with more than 5,000,000 cells.

### `value`

- `type`: `text`, `integer`, `decimal`, `percentage`, `date`, or `boolean`. In v1 this records what the value is; comparison behavior comes from `compare`. For instance, `type: "percentage"` does not strip a `%` sign — add the `percent_symbol` normalizer for that.
- `source_shape` and `component`: for values printed as one compound cell. Both keys must appear together. The shape fixes the expected cell text; the component picks which number to take:

| `source_shape`            | Cell text looks like                                                                                 | `component` values                                                                          |
| ------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `count_percentage`        | `12 (34%)`, `%` sign optional                                                                        | `count`, `percentage`                                                                       |
| `mean_standard_deviation` | `5.2 (0.8)`, optionally followed by a bracketed interval `5.2 (0.8) [4.0, 6.4]`                      | `mean`, `standard_deviation`, plus the optional `p_value`, `bayes_factor`                   |
| `range`                   | `10–20` (en dash, em dash, or hyphen)                                                                | `minimum`, `maximum`                                                                        |
| `estimate_interval`       | `0.261 (0.025) [0.213, 0.310]`, or `0.26 [0.22, 0.31]`, or `50 (45, 55)` (comma, semicolon, or dash) | `estimate`, `standard_error`, `lower`, `upper`, plus the optional `p_value`, `bayes_factor` |

Numbers inside these cells may carry a sign and use `.` or `,` as the decimal separator. The extractor tolerates trailing annotations such as `; P<0.001; BF>100`: it drops each short token compared against a number. In `mean_standard_deviation` and `estimate_interval` cells, those annotations can also become components: `p_value` matches `P<0.001` / `P=.025` forms, `bayes_factor` matches `BF>100` / `BF=12.3`, and the operators `<`, `>`, `=`, `≤`, and `≥` are all accepted. These optional components extract only when requested via `component`. They return the raw matched annotation after the `P`/`BF` token — `<0.001`, `=.025` — because an inequality is an interval statement, not a number; compare them with `exact`. A cell that matches the shape but carries no such annotation is `not_extracted` when the component is requested; the explanation says the annotation is absent. Inside `estimate_interval`, a parenthesized error yields `standard_error`; a bare interval offers `estimate`, `lower`, and `upper` only. If the cell text does not match the declared shape, the field is `not_extracted` and the explanation says so.

### `locate`

- `strategy`: `table_label`, `text_label`, or `text_span`. `table_label` matches row labels in detected tables. `text_label` matches label-and-value text lines: `Label: value` in one line, or a label ending in a colon with the value starting to its right on the same baseline. `text_label` takes `labels`, `section_labels` (nearest preceding heading), and `minimum_confidence`, but not `column_labels`. Use it when the value sits next to its label in running text; not for values buried in unlabeled sentences. Caveats by design: the value is the FIRST text line of the pair (multi-line values truncate). A value printed on the line BELOW its label does not pair. Lowering `minimum_confidence` invites silent wrong matches on other pages — quote the printed label exactly instead. `text_span` matches one anchor line — typically a section heading — and takes all the running prose after it, up to an end anchor: whole sections and paragraphs as plaintext, with provenance. Use it for the article body itself; NOT for a single labeled value, which belongs to `text_label` or `table_label`.
- `labels`: one or more accepted row-label aliases, each 1–256 characters. For `text_span` they name the START anchor. Required — unless the field declares `rows`, in which case `labels` must be absent: the row pattern replaces the alias list.
- `end_labels`: optional; `text_span` only, entries 1–256 characters. Aliases of the end anchor; the span ends before the first line that matches one (for example `References`). A line matches when it resembles the alias as a whole line, so a sentence that merely mentions the word does not close a span. Without `end_labels`, the span ends before the next line that looks like a heading of the same or higher level than its anchor. An anchor numbered `2.1` ends at the next `2.x` or `1` heading. An anchor that is no heading at all ends at the next heading of any level. A start anchor whose end never appears runs to the end of the document.
- `exclude_tables`: optional boolean, `text_span` only, default `true`. A span is prose, so lines that fall inside a geometrically ruled table stay out of its content — a span anchored on a table caption yields the caption and the paragraph after the table, not the table's numbers. Only tables whose rules the reader detected geometrically are excluded: a borderless "table" inferred from text alignment is often prose misread as a grid, and excluding on that inference would swallow the prose itself. Such tables keep their lines, and the explanation says so (as it does for a table with no locatable rectangle at all). The caption and any prose around the table are outside it and stay. Set `exclude_tables: false` to keep every line — tables included — which is how spans behaved before the flag existed.
- `section_labels`: optional; entries 1–256 characters.
- `column_labels`: optional; entries 1–256 characters. Rejected for `text_label` and `text_span`.
- `minimum_confidence`: 0–1, default `0.6`. The winning candidate's score breakdown — label score, section and column factors, reader confidence, final score, and the configured floor — appears in every extraction explanation and in the extraction artifact. Tune a threshold by reading the run, never by bisection.
- `value_pattern`: optional; a regular expression (string, at most 256 characters) that the candidate value must match for the field to extract. Use it for ids, urls, dates — anything with a known shape. The pattern is searched, not anchored, so write `^` and `$` when the whole value must conform. A best candidate whose value does not match makes the field `not_extracted`; the explanation names the pattern instead of handing a confidently wrong value to the comparison. An invalid pattern fails compilation with a diagnostic. Rejected for `text_span`: a span is prose, not a value of a known shape. The pattern runs on the cell's raw text, where `.` does not cross newlines — use `[\s\S]` when a match must span a wrapped cell.

How `table_label` finds a value:

- Candidates come from detected tables: the label sits in the first column, the value in another column of the same row. Row 0 supplies the column headers for context and is itself a data candidate, so a headerless table keeps its first row.
- Each alias is fuzzy-matched against the row label, case-insensitively. The best-scoring alias wins. Quote the printed form of a label: real documents glue footnote markers onto headers (`Junp`, `2025r`), and the scorer tolerates such close variants.
- `column_labels` are fuzzy-matched against the column's header cell. A candidate whose header matches poorly scores lower.
- `section_labels` are fuzzy-matched against the row's section context, which the reader produces itself. The reader splits merged grouped rows into one row per sub-entry and stamps the group name: a label cell `Group\nSub A\nSub B` over multi-line values becomes rows with section `Group`. The word-geometry rebuild for borderless tables stamps indentation-based parents the same way. A candidate whose section matches poorly loses up to 30 percent of its label score, which can pull it under `minimum_confidence`. Identically-labeled rows under different groups are exactly what `section_labels` is for.
- The best candidate must score at least `minimum_confidence`, or the field is `not_extracted` (with the best score against the floor in the explanation). If two candidates at different positions score within 0.03 of each other, the field is `ambiguous` instead of guessed. The explanation carries both the winner's and the runner-up's score and names the rival: its row label (or text line) and where it sits, so you can fix the tie without re-reading the evidence.
- When `pdftotext -layout` and the extractor's raw cell text disagree, trust the extractor (validated in the field): its cells come from the document's own geometry, while a text dump reflows wrapped and columnar text.

How `text_span` finds a value:

- Candidates are the document's text lines in reading order, page by page top to bottom. Each alias is fuzzy-scored against a line like any other strategy, and `section_labels` score against the nearest preceding heading, carried across pages. An anchor is a boundary, so a line longer than 64 characters is never a candidate: a sentence that merely mentions the heading cannot start a span.
- The span is every line after the anchor up to, not including, the end anchor, joined with single spaces. It crosses pages — a page boundary is a space, not a cut, the counterpart of `text_label`'s first-line truncation. Lines that repeat at the same height in the page margin on two or more pages — running heads and page-number folios — never appear in a span and never end one, so an article's prose reads clean while its furniture is ignored. Boilerplate pages contribute nothing because no line of them matches an anchor.
- With `exclude_tables` (the default), lines inside a geometrically ruled table are span furniture, not span prose: they never contribute to the span's text, though a line can still close it as an end anchor. Tables detected without rules keep their lines — an inferred grid over prose must not swallow the prose — and the explanation notes them.
- An anchor tie resolves toward the line that carries the requested alias as a COMPLETE token set. A shorter line whose tokens all sit inside your alias — a wrapped column header printing `Computational` / `Reproducibility` against the alias `2. Computational Reproducibility` — ties at 1.0 on token-set similarity; the tiebreak picks the line that carries every alias token. When it cannot separate the tied lines, the field is `ambiguous` and the explanation names the rival anchor (its text and page), not just the tied scores.
- A repeated anchor — a heading printed on every page — resolves only when every copy defines an identical span; otherwise the field is `ambiguous`, consistent with `text_label`.
- A matched anchor with no text between it and the end makes the field `not_extracted`, with an explanation naming the anchor. Long spans are the point: the value is never truncated.
- Evidence names the first and last covered line (`page` and `row`, plus `end_page` and `end_row`), carries the union bounding box of the covered lines when the span sits on a single page, and reports the matched anchor as `row_context`.

### Repeated rows (`rows`)

Some tables are one shape printed many times: invoice line items, period rows of a release table, per-precinct turnout rows. Instead of hand-authoring one field per row, the field declares a `rows` generator and the extractor creates one extraction per row whose label matches — bounded to label patterns, not general record alignment.

```json
{
  "key": "line_net",
  "label": "Line item net",
  "reference": { "pointer": "/lines" },
  "value": { "type": "decimal" },
  "locate": {
    "strategy": "table_label",
    "column_labels": ["Net"]
  },
  "compare": {
    "operator": "numeric",
    "normalizers": ["numeric_punctuation"]
  },
  "rows": { "label_pattern": "^Item (?P<item>\\d+)", "max_rows": 50 }
}
```

The document prints rows `Item 1 …`, `Item 2 …`, each with a `Net` column. The generator produces fields `line_net.1`, `line_net.2`, … — one comparison each, with the same location evidence a normal field carries. A period-keyed release table works the same way with `"label_pattern": "^(?P<period>20\\d{2})$"`.

- `label_pattern`: a regular expression (string, at most 256 characters, searched not anchored) that a candidate row label must match. With `rows` declared, the field has no `locate.labels`; the pattern is the row selector. The pattern must carry at least one capture group; an invalid pattern fails compilation, exactly like an invalid `value_pattern`. The pattern runs on the cell's raw text, where `.` does not cross newlines — wrapped labels (`"Long Rapids Township, Precinct\n1"`) need `[\s\S]` to capture across the line break, or the key stops at the wrap.
- `row_key_from`: which capture group supplies the row key — a named group (`"item"`) or a 1-based group index as a string (`"1"`). Defaults to the first group. Naming a group that does not exist fails compilation.
- `max_rows`: 1–200. The compiler cannot know the document, so generation happens at extraction time; this is the bound. More matching labels than `max_rows` generates only the first `max_rows` rows (in document order) plus a loud `not_compared` diagnostic (`ROW_LIMIT_EXCEEDED`). The dropped rows then surface as missing-row mismatches if the reference expects them.

How rows are generated:

- Candidates come from the field's locate strategy — `table_label` or `text_label` (`rows` is rejected for `text_span`, which is prose, not rows). The pattern matches the printed row label. A `locate.value_pattern` gates rows BEFORE component parsing: a candidate whose value breaks the declared shape is not a row at all (no key, no entry). This keeps one table's rows distinct when the same labels print under many tables. A reference key that loses its row this way surfaces as `ROW_NOT_IN_DOCUMENT`. `section_labels` and `column_labels` keep their usual role of gating candidates through `minimum_confidence`.
- Per matched label, only the BEST value column generates a row: among the columns of the same row that clear the floor, the highest label×column×confidence score wins — so `column_labels` genuinely selects the column, and `minimum_confidence` is not a per-column sieve. Near-identical headers (`Robustness I [4,000 iterations]` vs `Robustness II [5,000 iterations]`) fuzz-match too closely to separate on score. A tie inside the 0.03 ambiguity gap resolves exactly when the winner carries the requested `column_labels` as a complete token set and the rival does not. A tie that does not resolve makes the row `ambiguous` (its explanation names both columns and both scores), consistent with static fields. Without `column_labels`, a label's columns tie and every such row is `ambiguous`.
- The row key is the captured group, sanitized into the field-key charset: characters outside `[A-Za-z0-9_.-]` collapse to one underscore, leading and trailing separators drop, and 96 characters cap the length (`Region North/Upper` → `North_Upper`). The reference binds the sanitized key. Two rows that capture the same key do not silently merge: the second gets a `_2` suffix (then `_3`, …) and its explanation says so.
- Unlike a single field, a rows field never goes `ambiguous` on repeated labels — repetition is the point. Distinct rows live at distinct locations by construction; identical labels are the collision rule above, and the one ambiguity a rows field does carry is the per-label column tie described above. Every generated row's explanation carries its score breakdown (pattern, section and column factors, confidence, final score, and the configured floor), so a floor change reads its effect off the run.
- The same labels printed under MANY tables (an election statement's precinct rows, once per race block) still match once per table. Per-label best-column selection chooses the best column within a table, but cannot keep a different table out when its rows reuse the labels and its values fit the same shape. Raise `minimum_confidence` above the penalized off-table scores — a 0.9 floor keeps only exact-column matches at 1.0 — or scope with `value_pattern`. Rows still surface loudly (`ambiguous` rows, suffixed duplicates, `ROW_LIMIT_EXCEEDED`), never silently bind.

How the reference binds:

- `reference.pointer` must address a JSON **object** whose keys are the row keys: `/lines` above binds `line_net.1` to `reference["lines"]["1"]`. Compilation fails with `REFERENCE_POINTER_NOT_OBJECT` when the pointer resolves to something else. With `required: false` and a missing pointer, rows still extract and every comparison is `not_compared`.
- Each generated row compares against the object's key of the same name. A found row with no reference key is a `mismatch` (`ROW_NOT_IN_REFERENCE`); a reference key with no matching document row is a `mismatch` (`ROW_NOT_IN_DOCUMENT`). Both directions are loud — a row missing on either side can never pass silently.
- Row ids are stable across runs: a generated row's id is `sha256(specification_id:section_key:field_key:row_key)` — the field-id formula with the row key as a fourth segment. Re-running the same document with the same specification derives identical ids; renaming a captured key changes that row's id only. (As with all field ids, any specification change regenerates them.)

### `compare`

- `operator`: `exact`, `numeric`, `date`, or `boolean`.
  - `exact`: string equality after normalization.
  - `numeric`: both sides must parse as numbers after normalization. Pass if equal, or `|difference| <= absolute_tolerance`, or `|difference| / |reference| <= relative_tolerance`. Against a reference of 0, the relative rule passes only when the difference is 0.
  - `date`: each side parses independently, first as ISO-8601 (`YYYY-MM-DD`), then as a dotted day-first date (`02.09.2026`). Slashed month/day forms are deliberately unsupported; without a locale they are ambiguous. A side that parses under neither rule fails the comparison.
  - `boolean`: `true`/`yes`/`1` and `false`/`no`/`0`, case-insensitive. Anything else fails.
- `normalizers`: applied in order to both sides. Each may appear at most once; duplicates are rejected.

| Normalizer            | What it does                                                                                                                                                                                                                                                                                                                                                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `trim`                | Strips leading and trailing whitespace.                                                                                                                                                                                                                                                                                                                                                                             |
| `whitespace_collapse` | Collapses every run of whitespace to one space.                                                                                                                                                                                                                                                                                                                                                                     |
| `casefold`            | Lowercases (Unicode-aware).                                                                                                                                                                                                                                                                                                                                                                                         |
| `numeric_punctuation` | Strips spaces, then canonicalizes separators: when both `.` and `,` appear, the later one is the decimal point and the other is dropped (`1.234,56` and `1,234.56` both become `1234.56`); a separator that repeats is a thousands separator and is dropped; a single separator is read as a decimal point (`1,234` becomes `1.234`). Apply it to both sides of a comparison so the reading convention cancels out. |
| `percent_symbol`      | Removes `%` and trims.                                                                                                                                                                                                                                                                                                                                                                                              |
| `currency_symbol`     | Strips a leading `$`, `€`, `£`, or `¥` (`$1,234.56` becomes `1,234.56`) before numeric comparison.                                                                                                                                                                                                                                                                                                                  |

A leading currency symbol otherwise breaks the `numeric` operator — the value is not numeric after normalization. List `currency_symbol` ahead of `numeric_punctuation` so the separator rules run on bare digits.

- `absolute_tolerance`, `relative_tolerance`: numbers >= 0, permitted only with `operator: "numeric"`. The schema rejects them with any other operator.
- `severity`: `error` (default), `warning`, or `info` — the label a mismatch carries into the review summary.

## Diagnostics

Compilation failures report a list of diagnostics. Each has a `code` (`INVALID_SPECIFICATION_JSON`, `INVALID_SPECIFICATION_SCHEMA`, `INVALID_SPECIFICATION`, `INVALID_REFERENCE_DATA`, `REFERENCE_POINTER_NOT_FOUND`, `REFERENCE_POINTER_NOT_OBJECT`), a `specification_path` that points at the offending part of your JSON, a `message`, a `remediation_hint`, and where relevant the `reference_pointer` that failed. The upload page shows all of them.

Row generation reports at run time, through comparison rows rather than compilation: a found row with no reference key carries `ROW_NOT_IN_REFERENCE` in its explanation, a reference key with no document row carries `ROW_NOT_IN_DOCUMENT`, and a generator whose matches exceeded `max_rows` adds one `not_compared` row carrying `ROW_LIMIT_EXCEEDED`.

## Artifacts

The persisted artifacts (plan, evidence, extraction, result, progress) carry `artifact_schema_version: "2"`. The specification's `schema_version` (`"1"`) and the artifact version move independently. All page, row, and column indices in evidence and artifact locations are 0-based — page 0 is the first page, row 0 the first row — matching the `page_number` the reader stamps.

Each field in an extraction artifact carries its `field_key` and `section_key` alongside the hash `field_id`, so the artifact reads on its own without a join through the plan or comparison results. The keys are optional, so extraction artifacts written before they existed still load. An extraction generated by a `rows` field additionally carries its `row_key` and the `row_generator_id` of the plan field that generated it (both optional, both absent on ordinary fields and on older artifacts); the FieldComparison list gains one entry per row with `field_key` `<key>.<row>`.

Every extraction also carries the winning candidate's optional score breakdown — `label_score`, `section_factor`, `column_factor`, `final_score`, `minimum_confidence`, and `runner_up_score` on contested outcomes — mirroring the numbers the explanation prints. All keys are optional, so older artifacts still validate. On those contested outcomes the artifact also names the runner-up: `runner_up_text` (its row label, column header, or anchor line) and `runner_up_location` (where it sits).
