import type { EvidenceLocation } from "./index";

export interface ExtractedField {
  path: string;
  type: string;
  status: "extracted" | "missing" | "ambiguous" | "invalid" | "error";
  source: EvidenceLocation | null;
  message: string;
}

export interface StructuredExtraction {
  schema_version: "1";
  specification_name: string;
  specification_hash: string;
  document_hash: string;
  data: Record<string, unknown>;
  fields: ExtractedField[];
  pages: { width: number; height: number }[];
  created_at: string;
}

export interface SavedExtraction {
  id: string;
  document_filename: string;
  result: StructuredExtraction;
}

export interface ExtractionSummary {
  id: string;
  document_filename: string;
  specification_name: string;
  created_at: string;
  fields: number;
  issues: number;
}

export interface CheckRule {
  path: string;
  reference_pointer: string;
  absolute_tolerance?: number;
}

export interface CheckResult {
  path: string;
  reference_pointer: string;
  status: "match" | "mismatch" | "not_compared";
  actual: unknown;
  expected: unknown;
  message: string;
}
