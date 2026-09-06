---
name: schema-authoring
description: Write and verify a Janus comparison specification for values in a PDF, using supplied reference data or an extraction-only reference.
---

# Write a comparison specification

Produce `specification.json` for the user's requested fields and report the verified extraction
and comparison results. Work from the Janus directory, which contains `apps/server`.

Read the [specification guide](../../docs/comparison-specification-v1.md) and
[JSON schema](../../packages/contracts/schema/comparison-specification.v1.json) before drafting.
Read the [locator guide](../../docs/locators.md) for the relevant PDF layout, and the
[repeated-row guide](../../docs/repeated-rows.md) when labels follow a pattern.

## Establish the inputs

1. Identify the requested values and their reference dataset. Reuse information already supplied.
   Ask only for missing requirements that affect the result, such as permitted tolerances.
2. Read the PDF text and inspect its layout. `pdftotext -layout <pdf> -` helps find labels and cell
   formats. Use the reader's actual cell text when writing aliases, and check it against the PDF.
   Text dumps and inferred tables can both misread wrapped or columnar text.
3. Read the reference structure before choosing pointers. For CSV, positional pointers depend on
   row order; the two-column lookup view depends on unique first-column values. Escape `/` and `~`
   in pointer tokens. Keep supplied reference data unchanged unless the user requested an edit.
4. Use the requested comparison rules. Without an agreed tolerance, use exact numeric equality
   and state that choice. Choose normalizers only when their behavior fits the actual formats.

If the user wants extraction only, use an empty reference object with missing pointers and
`reference.required: false`. These fields extract normally and compare as `not_compared`.
If the user requests transcription from the PDF, label the resulting data as a transcription;
a match against that data is not an independent check of the document.

## Draft the fields

- Match the printed format: `table_label` for table values, `text_label` for same-line label/value
  pairs, and `text_span` for prose between anchors. Scanned labels require OCR outside Janus.
- Add `section_labels` for repeated labels under different groups and `column_labels` for multiple
  value columns. Use printed labels, including footnote markers. A group name belongs in section
  context; combining it with a row label can leave repeated rows tied.
- Use `source_shape` and `component` for compound cells. Select the shape by syntax: `0.261 (0.024)`
  uses `mean_standard_deviation` even when the statistic is not a mean. Keep field names semantic.
  One field should extract one required component. Compare inequality annotations with `exact`.
- Use a `rows` generator for labels sharing a pattern. Inspect sanitized keys, duplicate suffixes,
  and the selected column. A value pattern filters raw values before compound parsing. Patterns
  need `[\s\S]` to match across wrapped lines.
- Use `numeric` for amounts/counts and `exact` for identifiers whose text matters. Add
  `percent_symbol` for `%`, or `currency_symbol` before `numeric_punctuation` for currency symbols.
  A lone separator is decimal under `numeric_punctuation`: `1,234` becomes `1.234`. Applying the
  normalizer to both sides does not resolve differing locale conventions.
- `text_label` reads only the first value line and does not pair a value below its label.
  `text_span` excludes its anchor and, by default, lines inside ruled tables. Borderless inferences
  retain their lines. Review these limits when a requested value spans lines or pages.

Use the [purchase-order](../../examples/purchase-order/specification.json) or
[invoice](../../examples/invoice/specification.json) example for the JSON structure.

## Verify and report

Run the pipeline on the actual inputs. From the Janus directory, adapt the three paths:

```bash
cd apps/server && uv run python - <<'PY'
from pathlib import Path
from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.engine import ComparisonEngine
from janus.comparison.reference import ReferenceSource

doc = Path("../../examples/invoice/document.pdf")
spec = Path("../../examples/invoice/specification.json")
ref = Path("../../examples/invoice/reference.csv")
plan = SpecificationCompiler().compile(spec.read_bytes(), ReferenceSource.from_path(ref))
evidence = TextLayerDocumentReader().read(doc, "document")
extraction = SchemaExtractor().extract(plan, evidence)
for result in ComparisonEngine().compare(plan, extraction):
    print(result.field_key, result.extraction_status.value, result.status.value,
          repr(result.document_value), repr(result.reference_value), result.explanation)
PY
```

Fix compilation diagnostics first. For `not_extracted` or `ambiguous`, inspect the candidate,
its location, score breakdown, and runner-up before changing aliases or thresholds. Complete-token
matches can resolve near-identical headers; lowering confidence can instead admit unrelated rows.
Check mismatches against both sources before changing normalizers or tolerances.

Report the output path, each field's status and extracted value, and any unresolved limitations.
Include missing rows, ambiguous fields, and row-limit diagnostics. If a pipeline run is blocked,
state what prevented verification and label the specification unverified.
