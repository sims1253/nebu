# Locate PDF content

Each field's `locate` object selects a strategy. Use labels as they appear in the PDF text layer.
Inspect the extracted cell text when a text dump and the reader disagree; check the PDF itself
before accepting a match.

| Strategy      | Reads                                                                                 | Use for                        |
| ------------- | ------------------------------------------------------------------------------------- | ------------------------------ |
| `table_label` | First-column label and another cell in the same row                                   | Table values.                  |
| `text_label`  | `Label: value`, or a colon-ended label with a value to its right on the same baseline | Labeled text outside tables.   |
| `text_span`   | Lines after a start anchor and before an end anchor                                   | Prose sections and paragraphs. |

## Shared rules

`labels` lists accepted aliases, each 1 to 256 characters. At least one is required, except for
[rows generators](repeated-rows.md), which replace aliases with a pattern. `section_labels` is
optional and uses the same length limits. `column_labels` is optional for `table_label` only.

Aliases use case-insensitive fuzzy matching. Section and column matches each multiply the
score by a factor between 0.7 and 1.0. Reader confidence also contributes. The candidate must
reach `minimum_confidence` (0 to 1, default `0.6`). Otherwise the field is `not_extracted`.
Read the score breakdown and the actual candidate before changing this threshold.

Candidates at different positions within 0.03 of the best score can make a field `ambiguous`.
A complete-token match can resolve a tie when only one candidate contains all the requested
label and column tokens. For example, `Check II` can distinguish that header from `Check I`.
Unresolved ties report the rival's text, location, and score. Repeated text candidates can resolve
when their values agree; duplicated table rows remain ambiguous.

For `table_label` and `text_label`, `value_pattern` optionally requires the raw value to match
a regular expression of at most 256 characters. Patterns use search: add `^` and `$` for a
whole-value match. Invalid patterns fail compilation. `.` does not match a newline; use
`[\s\S]` for wrapped text. A static field whose best value fails the pattern is `not_extracted`.
[Rows generators](repeated-rows.md) filter values before generating rows.

## Tables

The first column supplies row labels; row 0 supplies column context and also remains a data
candidate, so headerless tables retain their first row. Use printed aliases, including footnote
markers such as `Junp` or `2025r`. Fuzzy matching tolerates close variants.

Use `column_labels` when a label has several value columns. Use `section_labels` when labels
repeat under different groups. The reader splits merged grouped rows into subrows and stamps
the group name as section context. For example, `Group\nSub A\nSub B` becomes two subrows under
`Group` when the value lines align. Borderless tables rebuilt from word geometry also use
indentation to identify parent groups. A section mismatch can lower the score by 30 percent.

Tables continued across pages can inherit the preceding header. The reader also rebuilds
borderless tables from text alignment and word positions; these inferred cells may have no
bounding box. Values can still compare, but missing boxes cannot support exact highlighting.

## Labeled text

`text_label` uses the nearest preceding heading as section context and has no column labels.
It reads only the first value line. Wrapped values truncate, and a value below its label does
not pair. Labels that exist only as pixels cannot match. Lower thresholds can admit unrelated
labels, so check the candidate's text and location before changing the threshold.

## Prose spans

For `text_span`, `labels` names the start anchor. Anchor candidates are at most 64 characters.
`section_labels` matches the preceding heading, carried across pages. `column_labels`,
`value_pattern`, and `rows` are not supported.

The span excludes the start and end anchors. It joins the intervening lines with single spaces,
crosses pages, and does not truncate. A matched anchor followed immediately by the end produces
`not_extracted`.

`end_labels` optionally lists end-anchor aliases, each 1 to 256 characters. Matching uses the
whole line, so a sentence mentioning "References" does not normally end the span. Without
`end_labels`, the next heading at the same or higher level ends the span: `2.1` ends at `2.2`
or `3`, but continues through `2.1.1`. A non-heading start ends at the next heading of any
level. If no end appears, the span runs to the end of the document.

Repeated lines at the same height in page margins and page-number folios are excluded from
span content and boundaries. A repeated start anchor resolves only when every copy defines
the same text; different spans produce `ambiguous`.

`exclude_tables` defaults to `true`. Lines inside geometrically ruled tables stay out of the
span, though they can still match its end anchor. Set it to `false` to keep table lines.
Borderless inferences and tables without known rectangles retain their lines, with a note in
the explanation. This avoids deleting prose that the reader misidentified as a table. A table
caption outside the rectangle follows normal span rules; if it is the start anchor, it is excluded.

Evidence records the first and last covered lines in `page`, `row`, `end_page`, and `end_row`.
A span on one page has a union bounding box when all its lines have boxes. `row_context` records
the matched start anchor.
