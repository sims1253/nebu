import { describe, expect, it } from "vitest";
import type { FieldComparison, ReviewResult } from "@janus/contracts";
import { exportFilename, exportReviewCsv, exportReviewJson } from "./index";

function comparison(overrides: Partial<FieldComparison> = {}): FieldComparison {
  return {
    id: "c1",
    field_id: "f1",
    section_key: "s",
    section_label: "Section",
    field_key: "f",
    field_label: "Field",
    reference_pointer: "/a",
    reference_value: 'He said "hello", politely\nand left',
    reference_value_present: true,
    extraction_status: "extracted",
    document_value: null,
    normalized_document_value: null,
    document_location: {
      page: 0,
      table_index: 0,
      row: 1,
      column: 2,
      raw_text: "x",
      cell_bbox: null,
      table_bbox: null,
    },
    column_context: null,
    row_context: null,
    status: "match",
    confidence: 1,
    severity: null,
    explanation: "",
    comparator: "exact",
    normalizers: [],
    absolute_tolerance: null,
    relative_tolerance: null,
    resolution: "pending",
    notes: null,
    original_document_value: null,
    original_document_location: null,
    ...overrides,
  };
}

function result(comparisons: FieldComparison[]): ReviewResult {
  return {
    artifact_schema_version: "2",
    review_id: "r",
    specification_id: "spec",
    reference_hash: "h",
    extraction_hash: "e",
    comparisons,
    summary: {
      total_fields: comparisons.length,
      matched: 0,
      mismatched: 0,
      not_compared: 0,
      ambiguous: 0,
      coverage_rate: 0,
      agreement_rate: null,
      by_severity: {},
    },
    processing_time_seconds: 0,
  };
}

describe("exportReviewCsv", () => {
  it("writes the header row", () => {
    expect(exportReviewCsv(result([])).split("\r\n")[0]).toContain("field_id");
  });

  it("quotes commas, quotes, and newlines with doubled quotes", () => {
    const csv = exportReviewCsv(result([comparison()]));
    const cell = '"He said ""hello"", politely\nand left"';
    expect(csv).toContain(cell);
  });

  it("serializes object reference values as JSON", () => {
    const csv = exportReviewCsv(
      result([comparison({ reference_value: { count: 5 } })]),
    );
    expect(csv).toContain('"{""count"":5}"');
  });

  it("reports pages as 1-based", () => {
    const csv = exportReviewCsv(result([comparison()]));
    const row = csv.split("\r\n")[1];
    expect(row?.split(",")).toContain("1");
  });
});

describe("exportReviewJson", () => {
  it("round-trips through JSON", () => {
    const original = result([comparison()]);
    expect(JSON.parse(exportReviewJson(original))).toEqual(original);
  });
});

describe("exportFilename", () => {
  it("sanitizes the document name and appends a timestamped extension", () => {
    const name = exportFilename(
      "Order #42!.pdf",
      "csv",
      new Date("2026-01-02T03:04:05Z"),
    );
    expect(name).toMatch(/^document_comparison_order_42_.*\.csv$/);
  });
});
