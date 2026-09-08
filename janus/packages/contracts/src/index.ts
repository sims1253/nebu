/**
 * Types shared by the web app and the FastAPI server.
 *
 * Specification types are generated from the canonical JSON Schema
 * (packages/contracts/scripts/generate-spec-types.mjs); everything else
 * describes the v2 review API by hand and mirrors the server's pydantic
 * models.
 */

export type {
  ComparisonSpecificationV1,
  FieldComparisonPolicy,
  SpecificationCompoundShape,
  SpecificationField,
  SpecificationNormalizer,
  SpecificationOperator,
  SpecificationSection,
  SpecificationSeverity,
  SpecificationValueType,
  FieldLocator,
} from "./comparison-specification.generated";

export interface HealthResponse {
  status: "healthy" | "error" | "starting";
  version: string;
}

export interface CompilationDiagnostic {
  code: string;
  specification_path: string;
  message: string;
  remediation_hint: string;
  reference_pointer?: string | null;
}

export interface ApiError {
  code: string;
  message: string;
  hint?: string;
  diagnostics?: CompilationDiagnostic[];
}

export type ReviewStatus =
  | "created"
  | "validating_inputs"
  | "reading_document"
  | "extracting_fields"
  | "comparing"
  | "ready"
  | "error";

export type ExtractionStatus =
  "extracted" | "not_extracted" | "ambiguous" | "error";
export type ComparisonStatus =
  "match" | "mismatch" | "not_compared" | "ambiguous";
export type Severity = "error" | "warning" | "info";
export type ComparisonOperator = "exact" | "numeric" | "date" | "boolean";
export type Normalizer =
  | "trim"
  | "whitespace_collapse"
  | "casefold"
  | "numeric_punctuation"
  | "percent_symbol";
export type Resolution =
  | "pending"
  | "accepted"
  | "reference_value_incorrect"
  | "incorrect_document_match"
  | "not_present_in_document";

export interface EvidenceLocation {
  page: number;
  table_index: number;
  row: number;
  column: number;
  raw_text: string;
  cell_bbox: Record<string, number> | null;
  table_bbox: Record<string, number> | null;
  /** Present for text_span regions: the last covered line, as page/row name the first. */
  end_page?: number | null;
  end_row?: number | null;
}

export interface FieldComparison {
  id: string;
  field_id: string;
  section_key: string;
  section_label: string;
  field_key: string;
  field_label: string;
  reference_pointer: string;
  reference_value: unknown;
  reference_value_present: boolean;
  extraction_status: ExtractionStatus;
  document_value: string | null;
  normalized_document_value: string | null;
  document_location: EvidenceLocation | null;
  column_context: string | null;
  row_context: string | null;
  status: ComparisonStatus;
  confidence: number;
  severity: Severity | null;
  explanation: string;
  comparator: ComparisonOperator;
  normalizers: Normalizer[];
  absolute_tolerance: number | null;
  relative_tolerance: number | null;
  resolution: Resolution;
  notes: string | null;
  original_document_value: string | null;
  original_document_location: EvidenceLocation | null;
}

export interface ReviewSummary {
  total_fields: number;
  matched: number;
  mismatched: number;
  not_compared: number;
  ambiguous: number;
  coverage_rate: number;
  /** Null when no field was actually compared. */
  agreement_rate: number | null;
  by_severity: Record<string, number>;
}

export interface ReviewResult {
  artifact_schema_version: "2";
  review_id: string;
  specification_id: string;
  reference_hash: string;
  extraction_hash: string;
  comparisons: FieldComparison[];
  summary: ReviewSummary;
  processing_time_seconds: number;
}

export interface ReviewMetadata {
  artifact_schema_version: "2";
  id: string;
  document_filename: string;
  document_format: "pdf";
  document_size_bytes: number;
  document_path: string;
  document_hash: string;
  specification_filename: string;
  specification_size_bytes: number;
  specification_path: string;
  specification_id: string;
  specification_version: "1";
  specification_hash: string;
  reference_filename: string;
  reference_format: "json" | "csv" | "xlsx";
  reference_size_bytes: number;
  reference_path: string;
  reference_hash: string;
  status: ReviewStatus;
  created_at: string;
  error_message: string | null;
}

export interface ReviewAnnotationRequest {
  comparison_id: string;
  resolution: Resolution;
  notes?: string | null;
  document_value?: string | null;
}

export interface ReviewAnnotationResponse {
  comparison_id: string;
  resolution: Resolution;
  notes: string | null;
  document_value: string | null;
  original_document_value: string | null;
}

export interface ReviewLocationHintRequest {
  comparison_id: string;
  page: number;
  table_index?: number;
  row?: number;
  column?: number;
  cell_bbox: Record<string, number>;
  raw_text?: string;
  notes?: string | null;
}

export interface ReviewLocationHintResponse {
  comparison_id: string;
  page: number;
  persisted: boolean;
}

export interface ValidationResponse {
  valid: boolean;
  specification_id: string;
  specification_version: string;
  reference_format: string;
  field_count: number;
}
