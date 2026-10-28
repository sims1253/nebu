// What the workbench receives from the server, as Effect Schemas.
//
// The first part mirrors the Python records in kittum.protocol, screening, judge, documents, and
// extraction field for field (snake_case, tuples as arrays). The last part holds what the screens
// need that the backend types do not carry yet; each says which work package should add it. When
// the server exposes these through OpenAPI, @kittum/contracts generates the same shapes and the
// mock below is replaced by the generated client.
import * as Schema from "effect/Schema";

const Id = Schema.String;
const Sha256 = Schema.String;
const Timestamp = Schema.String; // ISO 8601 UTC

// kittum.protocol

export const Stage = Schema.Literals(["title_abstract", "full_text"]);
export type Stage = typeof Stage.Type;

const PicoSlot = Schema.Literals([
  "population",
  "intervention",
  "comparator",
  "outcome",
  "study_type",
  "other",
]);

const ScreeningMode = Schema.Literals(["M1", "M2", "M3"]);

export const Criterion = Schema.Struct({
  id: Schema.String,
  slot: PicoSlot,
  text_de: Schema.String,
  text_en: Schema.String,
  decidable_at: Schema.Array(Stage),
  applies_to: Schema.Array(Schema.String),
  positive_examples: Schema.Array(Schema.String),
  negative_examples: Schema.Array(Schema.String),
});
export type Criterion = typeof Criterion.Type;

export const Scope = Schema.Struct({ id: Schema.String, label: Schema.String });
export type Scope = typeof Scope.Type;

const SearchTopic = Schema.Struct({
  id: Schema.String,
  kind: Schema.Literals(["rct_drug", "rct_indirect", "non_randomized", "other"]),
  mode: ScreeningMode,
  scopes: Schema.Array(Scope),
  criteria: Schema.Array(Criterion),
});

const ReviewProtocol = Schema.Struct({
  active_substance: Schema.String,
  version_label: Schema.String,
  topics: Schema.Array(SearchTopic),
});

/**
 * Words a review profile uses for protocol concepts. Not on the backend yet (WP-03): the core is
 * domain-neutral, and a profile (for example a regulatory one) may rename "population" or
 * "report". The screens read every such label from here and never hard-code one.
 */
export const ProfileLabels = Schema.Struct({
  scope: Schema.String,
  population: Schema.String,
  intervention: Schema.String,
  comparator: Schema.String,
  outcome: Schema.String,
  study_type: Schema.String,
  other: Schema.String,
  subgroup: Schema.String,
  report: Schema.String,
});
export type ProfileLabels = typeof ProfileLabels.Type;

const ProtocolProfile = Schema.Struct({ id: Schema.String, labels: ProfileLabels });

export const ProtocolVersion = Schema.Struct({
  protocol: ReviewProtocol,
  content_hash: Sha256,
  profile: ProtocolProfile,
});
export type ProtocolVersion = typeof ProtocolVersion.Type;

// kittum.judge

export const CriterionStatus = Schema.Literals(["met", "not_met", "unclear"]);
export type CriterionStatus = typeof CriterionStatus.Type;

export const CriterionVerdict = Schema.Struct({
  criterion_id: Schema.String,
  status: CriterionStatus,
  quote: Schema.NullOr(Schema.String),
  rationale: Schema.String,
  quote_verified: Schema.Boolean,
  downgraded_from: Schema.NullOr(CriterionStatus),
});
export type CriterionVerdict = typeof CriterionVerdict.Type;

// kittum.screening

export const Outcome = Schema.Literals([
  "include",
  "exclude",
  "unclear",
  "insufficient_information",
]);
export type Outcome = typeof Outcome.Type;

export const Phase = Schema.Literals(["provisional", "final"]);
export type Phase = typeof Phase.Type;

/** (start, end, tag): half-open code-point offsets; tags are `protocol:<criterion id>` or `entity:<type>`. */
export const Highlight = Schema.Tuple([Schema.Number, Schema.Number, Schema.String]);
export type Highlight = typeof Highlight.Type;

export const Decision = Schema.Struct({
  outcome: Outcome,
  reason_criterion_id: Schema.NullOr(Schema.String),
  evidence_anchor_ids: Schema.Array(Id),
  comment: Schema.NullOr(Schema.String),
});
export type Decision = typeof Decision.Type;

export const Reveal = Schema.Struct({
  criterion_id: Schema.String,
  quote: Schema.String,
  rationale: Schema.String,
  // Not on the backend record yet (WP-09): the AI's reading of the criterion, so the banner can
  // say "met" or "not met" instead of making the reviewer infer it.
  status: CriterionStatus,
});
export type Reveal = typeof Reveal.Type;

export const DecideResult = Schema.Struct({
  phase: Phase,
  reveal: Schema.NullOr(Reveal),
  recorded_at: Timestamp,
});
export type DecideResult = typeof DecideResult.Type;

export const ScopeStatus = Schema.Struct({
  scope_id: Schema.String,
  stage: Stage,
  screened: Schema.Number,
  remaining: Schema.Number,
  conflicts: Schema.Number,
});
export type ScopeStatus = typeof ScopeStatus.Type;

// kittum.documents

export const Box = Schema.Struct({
  page: Schema.Number, // 1-based
  x0: Schema.Number,
  y0: Schema.Number,
  x1: Schema.Number,
  y1: Schema.Number,
});
export type Box = typeof Box.Type;

const Word = Schema.Struct({ start: Schema.Number, end: Schema.Number, box: Box });

const Document = Schema.Struct({
  id: Id,
  artifact_hash: Sha256,
  media_type: Schema.String,
  page_count: Schema.NullOr(Schema.Number),
});

const TextQuote = Schema.Struct({
  exact: Schema.String,
  prefix: Schema.String,
  suffix: Schema.String,
});

export const Anchor = Schema.Struct({
  id: Id,
  layer_id: Id,
  quote: TextQuote,
  start: Schema.Number,
  end: Schema.Number,
  boxes: Schema.Array(Box),
});
export type Anchor = typeof Anchor.Type;

// kittum.extraction

export const Policy = Schema.Literals(["critical", "standard", "descriptive"]);
export type Policy = typeof Policy.Type;

export const ProposalStatus = Schema.Literals([
  "extracted",
  "missing",
  "ambiguous",
  "invalid",
  "error",
]);
export type ProposalStatus = typeof ProposalStatus.Type;

export const SemanticKey = Schema.Struct({
  study_id: Id,
  report_id: Id,
  subpopulation: Schema.String,
  analysis_set: Schema.String,
  data_cut: Schema.String,
  arm: Schema.String,
  endpoint: Schema.String,
  timepoint: Schema.String,
  quantity: Schema.String,
});
export type SemanticKey = typeof SemanticKey.Type;

export const FieldSpec = Schema.Struct({
  quantity: Schema.String,
  unit: Schema.NullOr(Schema.String),
  policy: Policy,
  description: Schema.String,
});
export type FieldSpec = typeof FieldSpec.Type;

const ValidatorResult = Schema.Struct({
  validator: Schema.String,
  passed: Schema.Boolean,
  message: Schema.NullOr(Schema.String),
});

export const Proposal = Schema.Struct({
  key: SemanticKey,
  value: Schema.NullOr(Schema.String),
  unit: Schema.NullOr(Schema.String),
  status: ProposalStatus,
  path: Schema.Literals(["janus", "llm"]),
  anchor_ids: Schema.Array(Id),
  validators: Schema.Array(ValidatorResult),
});
export type Proposal = typeof Proposal.Type;

// Workbench additions: not on the backend records yet.

/** Bibliographic facts the queue shows above the abstract (WP-07 corpus, served by WP-15). */
const RecordMeta = Schema.Struct({
  pmid: Schema.NullOr(Schema.String),
  doi: Schema.NullOr(Schema.String),
  journal: Schema.String,
  year: Schema.Number,
  publication_types: Schema.Array(Schema.String),
  registry_ids: Schema.Array(Schema.String),
  /** Highlights in the title; `QueueItem.highlights` covers the abstract. */
  title_highlights: Schema.Array(Highlight),
  /** Structured-abstract labels as (label, start, end) code-point ranges of `text`. */
  sections: Schema.Array(Schema.Tuple([Schema.String, Schema.Number, Schema.Number])),
});

/** `kittum.screening.QueueItem` plus the record facts. Never carries AI output or sentinel flags. */
export const QueueItem = Schema.Struct({
  assignment_id: Id,
  stage: Stage,
  scope_id: Schema.String,
  title: Schema.String,
  text: Schema.String,
  highlights: Schema.Array(Highlight),
  revision: Schema.Number,
  record: RecordMeta,
});
export type QueueItem = typeof QueueItem.Type;

/** Per-item timing the workbench reports with each decision (DESIGN.md "Metrics"). */
export const DecisionTiming = Schema.Struct({
  shown_at: Timestamp,
  first_key_at: Schema.NullOr(Timestamp),
  decided_at: Timestamp,
  reveal_shown_at: Schema.NullOr(Timestamp),
});
export type DecisionTiming = typeof DecisionTiming.Type;

export const ProjectSummary = Schema.Struct({
  id: Schema.String,
  active_substance: Schema.String,
  indication: Schema.String,
  review: Schema.String,
  reveal: Schema.Literals(["never", "on_disagreement", "always"]),
  /** The scope this reviewer screens now. */
  scope_id: Schema.NullOr(Schema.String),
});
export type ProjectSummary = typeof ProjectSummary.Type;

export const PageSize = Schema.Struct({
  page: Schema.Number,
  width: Schema.Number,
  height: Schema.Number,
});
export type PageSize = typeof PageSize.Type;

/** A parsed layer without its words: the viewer loads words per page. */
export const LayerSummary = Schema.Struct({
  id: Id,
  document_id: Id,
  recipe_hash: Sha256,
  output_hash: Sha256,
  text: Schema.String,
  pages: Schema.Array(PageSize),
});
export type LayerSummary = typeof LayerSummary.Type;

/** One rendered page: the server's PNG and the layer's words on it. */
export const PageLayer = Schema.Struct({
  page: Schema.Number,
  width: Schema.Number,
  height: Schema.Number,
  image_url: Schema.String,
  words: Schema.Array(Word),
});
export type PageLayer = typeof PageLayer.Type;

const ReportSummary = Schema.Struct({
  id: Id,
  study_id: Id,
  study_label: Schema.String,
  title: Schema.String,
  citation: Schema.String,
  licence: Schema.String,
});

/** A full text to screen: the report, its layer, and the protocol-term mentions per criterion. */
export const FullTextAssignment = Schema.Struct({
  assignment_id: Id,
  scope_id: Schema.String,
  revision: Schema.Number,
  report: ReportSummary,
  document: Document,
  layer: LayerSummary,
  mentions: Schema.Array(Schema.Tuple([Schema.String, Schema.Array(Anchor)])),
});
export type FullTextAssignment = typeof FullTextAssignment.Type;

/** One field to review: the proposals of both paths for one semantic key. */
export const ReviewField = Schema.Struct({
  id: Id,
  spec: FieldSpec,
  key: SemanticKey,
  proposals: Schema.Array(Proposal),
  accepted: Schema.NullOr(Schema.Literals(["janus", "llm"])),
});
export type ReviewField = typeof ReviewField.Type;

export const ExtractionReview = Schema.Struct({
  report: ReportSummary,
  document: Document,
  layer: LayerSummary,
  anchors: Schema.Array(Anchor),
  fields: Schema.Array(ReviewField),
});
export type ExtractionReview = typeof ExtractionReview.Type;
