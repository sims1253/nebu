import type { FieldComparison, ReviewResult } from "@janus/contracts";

/** The parts of a review result that the CSV and JSON exports write out. */
export type ReviewExportData = Pick<
  ReviewResult,
  "artifact_schema_version" | "specification_id"
> & {
  comparisons: FieldComparison[];
};

function csvCell(value: unknown): string {
  const text =
    value === null || value === undefined
      ? ""
      : typeof value === "object"
        ? JSON.stringify(value)
        : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function exportReviewCsv(result: ReviewExportData): string {
  const headers = [
    "artifact_schema_version",
    "specification_id",
    "field_id",
    "section_key",
    "section_label",
    "field_key",
    "field_label",
    "reference_pointer",
    "reference_value",
    "extraction_status",
    "document_value",
    "document_page",
    "document_table",
    "document_row",
    "document_column",
    "comparison_status",
    "confidence",
    "comparator",
    "absolute_tolerance",
    "relative_tolerance",
    "resolution",
    "notes",
    "original_document_value",
  ];
  const rows = result.comparisons.map((item) => [
    result.artifact_schema_version,
    result.specification_id,
    item.field_id,
    item.section_key,
    item.section_label,
    item.field_key,
    item.field_label,
    item.reference_pointer,
    item.reference_value,
    item.extraction_status,
    item.document_value,
    item.document_location ? item.document_location.page + 1 : null,
    item.document_location?.table_index,
    item.document_location?.row,
    item.document_location?.column,
    item.status,
    item.confidence,
    item.comparator,
    item.absolute_tolerance,
    item.relative_tolerance,
    item.resolution,
    item.notes,
    item.original_document_value,
  ]);
  return [headers, ...rows]
    .map((row) => row.map(csvCell).join(","))
    .join("\r\n");
}

export function exportReviewJson(result: ReviewExportData): string {
  return JSON.stringify(result, null, 2);
}

export function exportFilename(
  name: string,
  extension: "csv" | "json",
  now = new Date(),
): string {
  const safe =
    name
      .replace(/\.[^.]+$/, "")
      .replace(/[^a-z0-9]+/gi, "_")
      .replace(/^_+|_+$/g, "")
      .toLowerCase() || "review";
  const timestamp = now.toISOString().replace(/[:.]/g, "-");
  return `document_comparison_${safe}_${timestamp}.${extension}`;
}
