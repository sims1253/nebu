/**
 * Generated from schema/comparison-specification.v1.json. Do not edit.
 * Regenerate with `bun run contracts:generate`.
 *
 * Conditional constraints (value.component per source_shape, tolerances only
 * with the numeric operator) are enforced against the same schema by the
 * server and are not expressible in these static types.
 */

export type SpecificationValueType = "text" | "integer" | "decimal" | "percentage" | "date" | "boolean";

export type SpecificationCompoundShape = "count_percentage" | "mean_standard_deviation" | "range" | "estimate_interval";

export type SpecificationNormalizer = "trim" | "whitespace_collapse" | "casefold" | "numeric_punctuation" | "percent_symbol" | "currency_symbol";

export type SpecificationOperator = "exact" | "numeric" | "date" | "boolean";

export type SpecificationSeverity = "error" | "warning" | "info";

export interface ComparisonSpecificationV1 {
  $schema?: string;
  schema_version: "1";
  name: string;
  sections: SpecificationSection[];
}

export interface SpecificationSection {
  key: string;
  label: string;
  fields: SpecificationField[];
}

export interface SpecificationField {
  key: string;
  label: string;
  reference: {
    pointer: string;
    required?: boolean;
  };
  value: {
    type: SpecificationValueType;
    source_shape?: SpecificationCompoundShape;
    component?: string;
  };
  locate: FieldLocator;
  compare: FieldComparisonPolicy;
  rows?: FieldRowGenerator;
}

export interface FieldRowGenerator {
  label_pattern: string;
  max_rows: number;
  row_key_from?: string;
}

export interface FieldLocator {
  strategy: "table_label" | "text_label" | "text_span";
  labels?: string[];
  end_labels?: string[];
  exclude_tables?: boolean;
  section_labels?: string[];
  column_labels?: string[];
  minimum_confidence?: number;
  value_pattern?: string;
}

export interface FieldComparisonPolicy {
  operator: SpecificationOperator;
  normalizers?: SpecificationNormalizer[];
  absolute_tolerance?: number;
  relative_tolerance?: number;
  severity?: SpecificationSeverity;
}

