# Janus design

The review screen has two panes: a table of fields on the left, the source PDF on the right. Select a row. The PDF opens on the page the value came from; a panel beside it shows the confidence, the comparison explanation, the extraction status, and the row and column context.

The reviewer works from the table. Each row shows the field, the reference value, the document value, a status, and a resolution menu (`Accept`, `Reference value incorrect`, `Incorrect document match`, `Not present in document`). Rows are compact, so many fields fit on screen. Buttons above the table filter by status and export the result as CSV or JSON.

Statuses name the failure. `not_compared` and `ambiguous` never show as `mismatch`: a field the machine could not read is not a field that disagrees. Every status prints its word; color is secondary. The table stays readable in grayscale and for color-blind readers.

Rows work from the keyboard. Each row takes focus; Enter or Space selects it. The theme follows the system light or dark setting, with a toggle in the header. The choice persists.
