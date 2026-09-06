# Repeated rows

A `rows` rule creates one field per matching label, useful for invoice lines, reporting periods,
or precincts. It supports `table_label` and `text_label`; it does not align arbitrary records.

```json
{
  "key": "line_net",
  "label": "Line item net",
  "reference": { "pointer": "/lines" },
  "value": { "type": "decimal" },
  "locate": { "strategy": "table_label", "column_labels": ["Net"] },
  "compare": { "operator": "numeric", "normalizers": ["numeric_punctuation"] },
  "rows": { "label_pattern": "^Item (?P<item>\\d+)", "max_rows": 50 }
}
```

For labels `Item 1` and `Item 2`, this produces `line_net.1` and `line_net.2`.
The reference can be `{"lines": {"1": 12.5, "2": 20}}`. Each generated field retains its location.
A period pattern could instead be `^(?P<period>20\d{2})$`.

## Options

| Key             | Rule                                                                                                                                  |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `label_pattern` | Required regular expression, at most 256 characters, with at least one capture group. Replaces `locate.labels`, which must be absent. |
| `row_key_from`  | Capture-group name or 1-based group index as a string; defaults to `"1"`. The group must exist.                                       |
| `max_rows`      | Required limit, 1 to 200, applied during extraction.                                                                                  |

Patterns search raw row-label text. Use anchors for whole-label matching. `.` does not cross
newlines; use `[\s\S]` when a captured key spans a wrapped label. Invalid patterns fail compilation.

The captured key replaces runs outside `[A-Za-z0-9_.-]` with `_`, trims leading/trailing separators,
and truncates to 96 characters. For example, captured `North/Upper` becomes `North_Upper`.
Repeated keys receive `_2`, `_3`, and further suffixes; their explanations report the collision.

## Candidate selection

`locate.value_pattern` filters raw values before component parsing or row generation. A rejected
value produces no row. Section and column labels affect the score through `minimum_confidence`.

For each matched row, only the best value column generates a field. Columns within the 0.03 tie
gap produce `ambiguous` unless only one carries the requested column labels as complete tokens.
Without column labels, equally scored columns remain ambiguous. Repeated labels at different
row locations generate separate rows; they do not trigger the static-field ambiguity rule.

The same labels under several tables can still generate rows from each table. Column selection
only chooses within a row. Inspect section context, value patterns, and score breakdowns to
exclude unrelated tables. A high threshold can reject penalized matches, but does not prove
that every retained row belongs to the intended table.

## Reference matching

`reference.pointer` must address an object keyed by the sanitized row keys. Any other resolved
type fails compilation with `REFERENCE_POINTER_NOT_OBJECT`. With `required: false` and a missing
pointer, rows still extract and their comparisons are `not_compared`.

Each generated row compares with the reference key of the same name:

- A document row without a reference key is a `mismatch` with `ROW_NOT_IN_REFERENCE`.
- A reference key without a document row is a `mismatch` with `ROW_NOT_IN_DOCUMENT`.
- Matches beyond `max_rows` are omitted after the first rows in document order. An additional
  `not_compared` row reports `ROW_LIMIT_EXCEEDED`; omitted expected rows also appear as missing.

Generated IDs use `sha256(specification_id:section_key:field_key:row_key)`. The same specification
and captured key produce the same ID on another run. A captured-key change affects that row's ID;
any specification change regenerates all field IDs.
