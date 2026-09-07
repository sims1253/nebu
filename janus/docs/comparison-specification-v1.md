# Comparison specification v1

A specification names the reference value, the PDF location, and the comparison rule for each
field. The server validates it against the [JSON schema](../packages/contracts/schema/comparison-specification.v1.json),
which rejects unknown keys. Use the [locator guide](locators.md) to choose where to read and the
[repeated-row guide](repeated-rows.md) when labels follow a pattern.

## Example

The [purchase-order example](../examples/purchase-order) compares `PO-1042` with `"PO-1042"`
and `1,275.50` with `1275.5`. Both fields match. The numeric rule also allows a difference of `0.01`.

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

## Structure

| Key              | Requirement                                                                         |
| ---------------- | ----------------------------------------------------------------------------------- |
| `schema_version` | `"1"`.                                                                              |
| `name`           | 1 to 200 characters.                                                                |
| `sections`       | At least one section, each with `key`, `label`, and at least one entry in `fields`. |
| `$schema`        | Optional schema identifier. The example URL is an identifier, not a hosted schema.  |

Section and field keys match `^[A-Za-z0-9][A-Za-z0-9_.-]*$` and have at most 128 characters.
Labels have 1 to 256 characters. Section keys must be unique; field keys must be unique within
their section. Duplicate keys produce `INVALID_SPECIFICATION`.

Each field has `key`, `label`, `reference`, `value`, `locate`, and `compare`.
An optional [rows generator](repeated-rows.md) produces one field per matching row.

## Reference data

`reference.pointer` is an RFC 6901 JSON Pointer, at most 2048 characters, into the normalized
reference tree. Escape `/` as `~1` and `~` as `~0` within a key. Array indexes start at zero.

| Input | Normalized tree                           | Example pointer               |
| ----- | ----------------------------------------- | ----------------------------- |
| JSON  | Original tree                             | `/order/id`                   |
| CSV   | `{"rows": [...]}`                         | `/rows/0/total`               |
| XLSX  | `{"sheets": {"Sheet1": {"rows": [...]}}}` | `/sheets/Sheet1/rows/0/total` |

CSV values stay strings. CSV and XLSX require non-empty, unique headers. The server limits
XLSX data rows to 5,000,000 cells. A two-column CSV also provides `/lookup/<first-column-value>/value`;
this view binds by key when exports reorder rows. Later duplicate keys replace earlier ones.
Use it only when first-column values identify records uniquely.

`reference.required` defaults to `true`. A missing required pointer fails compilation with
`REFERENCE_POINTER_NOT_FOUND`. With `required: false`, the field can extract but its comparison
is `not_compared`. This supports extraction-only work with an empty reference object.

## Values and compound cells

`value.type` is `text`, `integer`, `decimal`, `percentage`, `date`, or `boolean`. It describes the
value; `compare.operator` determines comparison behavior. `percentage` alone does not remove `%`.

For a cell containing several numbers, set both `value.source_shape` and `value.component`.
Choose the shape by the printed format, then declare one field per required component.
Without these keys, the operator receives the whole cell.

| Shape                     | Printed form                                                          | Components                                                                |
| ------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `count_percentage`        | `12 (34%)`, with optional `%`                                         | `count`, `percentage`                                                     |
| `mean_standard_deviation` | `5.2 (0.8)`, optionally with `[4.0, 6.4]`                             | `mean`, `standard_deviation`, `p_value`, `bayes_factor`                   |
| `range`                   | `10-20`, also accepting en/em dashes                                  | `minimum`, `maximum`                                                      |
| `estimate_interval`       | `0.261 (0.025) [0.213, 0.310]`, `0.26 [0.22, 0.31]`, or `50 (45, 55)` | `estimate`, `standard_error`, `lower`, `upper`, `p_value`, `bayes_factor` |

Parenthesized intervals accept commas, semicolons, or dashes. Numbers accept signs and either
`.` or `,` as the decimal separator. A space after the separator, such as `0. 261`, is tolerated.
`standard_error` requires the printed error before the interval. A bare interval has only the
estimate and bounds. A shape mismatch or absent requested component produces `not_extracted`.

Trailing annotations such as `; P<0.001; BF>100` are ignored when extracting the main numbers.
For the two statistical shapes, `p_value` and `bayes_factor` can instead extract these annotations.
They accept `<`, `>`, `=`, `≤`, or `≥` and return the text after `P` or `BF`, such as `<0.001` or
`=.025`. Compare these with `exact`; the inequality is part of the value.

## Comparison rules

| `compare.operator` | Rule                                                                                                                        |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `exact`            | String equality after normalization.                                                                                        |
| `numeric`          | Finite numbers after normalization; equality or a permitted tolerance. Invalid or out-of-range numbers fail the comparison. |
| `date`             | Each side parses independently as ISO-8601 or dotted day-first (`02.09.2026`). Slashed month/day dates are unsupported.     |
| `boolean`          | Case-insensitive `true`/`yes`/`1` or `false`/`no`/`0`. Other values fail.                                                   |

`absolute_tolerance` and `relative_tolerance` must be non-negative and are allowed only with
`numeric`. A comparison passes when equal, when `|difference| <= absolute_tolerance`, or when
`|difference| / |reference| <= relative_tolerance`. For reference zero, the relative rule passes
only on equality. Choose tolerances from the review requirements.

`severity` is `error` by default, or `warning` or `info`. It labels mismatches in the summary.

`normalizers` apply in order to both values; duplicate entries are rejected.

| Normalizer            | Effect                                                                                                                                                                |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `trim`                | Strip leading and trailing whitespace.                                                                                                                                |
| `whitespace_collapse` | Replace whitespace runs with one space.                                                                                                                               |
| `casefold`            | Unicode case folding.                                                                                                                                                 |
| `percent_symbol`      | Remove `%` and trim.                                                                                                                                                  |
| `currency_symbol`     | Strip leading `$`, `€`, `£`, or `¥`; place before `numeric_punctuation`.                                                                                              |
| `numeric_punctuation` | Remove spaces. With both `.` and `,`, treat the last as the decimal separator and remove the other. Remove a repeated separator. Treat a single separator as decimal. |

`numeric_punctuation` converts both `1.234,56` and `1,234.56` to `1234.56`, but converts `1,234`
to `1.234`. Applying it to both values does not resolve different locale conventions. Check
ambiguous formats against the source data before relying on a result.

## Diagnostics and artifacts

Compilation diagnostics include `code`, `specification_path`, `message`, `remediation_hint`,
and, when relevant, `reference_pointer`. Codes are `INVALID_SPECIFICATION_JSON`,
`INVALID_SPECIFICATION_SCHEMA`, `INVALID_SPECIFICATION`, `INVALID_REFERENCE_DATA`,
`REFERENCE_POINTER_NOT_FOUND`, and `REFERENCE_POINTER_NOT_OBJECT`. The upload page displays them.
[Row diagnostics](repeated-rows.md#reference-matching) appear in the comparison result.

Plan, evidence, extraction, and result artifacts carry `artifact_schema_version: "2"`,
independent of specification version `"1"`. Evidence page, row, and column indexes are zero-based;
the UI and CSV page column show pages starting at one.

Extraction artifacts include optional `field_key` and `section_key` alongside `field_id`.
Generated rows also include `row_key` and `row_generator_id`. Optional score details are
`label_score`, `section_factor`, `column_factor`, `final_score`, `minimum_confidence`, and
`runner_up_score`. Contested results can include `runner_up_text` and `runner_up_location`.
Older artifacts without these optional details still load.
