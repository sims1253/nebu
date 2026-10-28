// What the screens work with, as Effect Schemas.
//
// Server records come from @kittum/contracts, generated from the server's OpenAPI document, so the
// Pydantic models stay the single source of truth. This file adds what the screens compose from
// several responses (a full-text assignment, a page with its words), the protocol as the screens
// read it, and the extraction records, which the server does not serve yet (WP-11). Both
// Workbench implementations, the API one and the mock, return these shapes.
import { KittumApi } from "@kittum/contracts";
import * as Schema from "effect/Schema";

const Id = Schema.String;
const Sha256 = Schema.String;

// Server records, as generated.

export const Stage = Schema.Literals(["title_abstract", "full_text"]);
export type Stage = KittumApi.Stage;
export type Phase = KittumApi.Phase;
export type CriterionStatus = KittumApi.CriterionStatus;
export type Outcome = KittumApi.Kittum__screening__types__Outcome;
export type RevealSetting = KittumApi.RevealSetting;
export type Role = KittumApi.MemberRole;

export const QueueItem = KittumApi.QueueItem;
/** Never carries AI output before the reviewer's own provisional decision, or sentinel flags. */
export type QueueItem = KittumApi.QueueItem;
export const DecideResult = KittumApi.DecideResult;
export type DecideResult = KittumApi.DecideResult;
export type Reveal = KittumApi.Reveal;
export type Highlight = KittumApi.Highlight;
export type ScopeStatus = KittumApi.ScopeStatus;
export type StageInfo = KittumApi.StageInfo;
export type ProjectSummary = KittumApi.ProjectSummary;
export type Member = KittumApi.Member;
export type AdjudicationItem = KittumApi.AdjudicationItem;
export type AdjudicationResult = KittumApi.AdjudicationResult;
export type ReviewerColumn = KittumApi.ReviewerColumn;
export type CriterionVerdict = KittumApi.CriterionVerdict;
export type Progress = KittumApi.Progress;
export type StageProgressView = KittumApi.StageProgress;
export type SearchExecution = KittumApi.SearchExecution;
export type CorpusView = KittumApi.CorpusView;
export type CorpusReport = KittumApi.CorpusReport;
export type SourceRecord = KittumApi.SourceRecord;
export type LinkProposal = KittumApi.LinkProposal;
export type IdentityRevision = KittumApi.IdentityRevision;
export type LedgerSummary = KittumApi.LedgerSummary;
export type RenderedReport = KittumApi.RenderedArtifact;
export type SealedBundle = KittumApi.SealedBundle;
type CriterionDecision = KittumApi.CriterionDecision;

/** A decision as the screens build it: every field the server reads, none left to defaults. */
export interface Decision {
  readonly outcome: Outcome;
  readonly reason_criterion_id: string | null;
  readonly evidence_anchor_ids: ReadonlyArray<string>;
  readonly comment: string | null;
  /** Full text: one reading per criterion, with its evidence. */
  readonly criteria?: ReadonlyArray<CriterionDecision>;
}

/** Per-item timing the workbench reports with each decision (DESIGN.md "Metrics"). */
export interface DecisionTiming {
  readonly shown_at: string;
  readonly first_key_at: string | null;
  readonly decided_at: string;
  readonly reveal_shown_at: string | null;
}

// The protocol as the screens read it: criteria per scope, worded in English and German.

const PicoSlot = Schema.Literals([
  "population",
  "intervention",
  "comparator",
  "outcome",
  "study_type",
  "other",
]);

export const Criterion = Schema.Struct({
  id: Schema.String,
  slot: PicoSlot,
  polarity: Schema.Literals(["include", "exclude"]),
  text_en: Schema.String,
  /** The German wording, or the English one when the protocol has none. */
  text_de: Schema.String,
  decidable_at: Schema.Array(Stage),
  /** Scope IDs; a criterion for a whole search topic lists every scope of the topic. */
  applies_to: Schema.Array(Schema.String),
  keywords: Schema.Array(Schema.String),
  positive_examples: Schema.Array(Schema.String),
  negative_examples: Schema.Array(Schema.String),
});
export type Criterion = typeof Criterion.Type;

const Scope = Schema.Struct({ id: Schema.String, label: Schema.String });

const SearchTopic = Schema.Struct({
  id: Schema.String,
  label: Schema.String,
  kind: Schema.String,
  mode: Schema.Literals(["M1", "M2", "M3"]),
  mode_rationale: Schema.NullOr(Schema.String),
  scopes: Schema.Array(Scope),
  criteria: Schema.Array(Criterion),
  sentinel_percent: Schema.String,
  random_audit_percent: Schema.String,
  sources: Schema.Array(
    Schema.Struct({ id: Schema.String, kind: Schema.String, name: Schema.String }),
  ),
});
export type SearchTopic = typeof SearchTopic.Type;

const Question = Schema.Struct({
  id: Schema.String,
  label: Schema.String,
  population: Schema.String,
  intervention: Schema.String,
  comparators: Schema.Array(Schema.String),
});

const ReviewProtocol = Schema.Struct({
  title: Schema.String,
  version_label: Schema.String,
  questions: Schema.Array(Question),
  outcomes: Schema.Array(
    Schema.Struct({ id: Schema.String, category: Schema.String, name: Schema.String }),
  ),
  topics: Schema.Array(SearchTopic),
});

/**
 * Words a review profile uses for core concepts. The core is domain-neutral; a profile may rename
 * "question" (a subpopulation), "outcome" (an endpoint), and so on. The screens read every such
 * label from here and never hard-code one.
 */
export const ProfileLabels = Schema.Struct({
  scope: Schema.String,
  population: Schema.String,
  intervention: Schema.String,
  comparator: Schema.String,
  outcome: Schema.String,
  study_type: Schema.String,
  other: Schema.String,
  report: Schema.String,
  study: Schema.String,
  search_topic: Schema.String,
});
export type ProfileLabels = typeof ProfileLabels.Type;

const ProtocolProfile = Schema.Struct({ id: Schema.NullOr(Schema.String), labels: ProfileLabels });

export const ProtocolVersion = Schema.Struct({
  version_id: Id,
  protocol: ReviewProtocol,
  content_hash: Sha256,
  published_at: Schema.NullOr(Schema.String),
  profile: ProtocolProfile,
});
export type ProtocolVersion = typeof ProtocolVersion.Type;

// kittum.documents, as the viewer uses them

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
  study_label: Schema.String,
  title: Schema.String,
  citation: Schema.String,
  licence: Schema.String,
});

/** The next full text in the reviewer's queue: the record, its layer, and the protocol-term
 * mentions per criterion in document order. */
export const FullTextAssignment = Schema.Struct({
  assignment_id: Id,
  scope_id: Schema.String,
  etag: Schema.String,
  phase: Schema.Literals(["provisional", "final"]),
  /** The AI's reading, when an earlier provisional decision of this reviewer revealed it. */
  reveal: Schema.NullOr(
    Schema.Struct({
      criterion_id: Schema.String,
      status: Schema.Literals(["met", "not_met", "unclear"]),
      quote: Schema.String,
      rationale: Schema.String,
    }),
  ),
  report: ReportSummary,
  document: Document,
  layer: LayerSummary,
  mentions: Schema.Array(Schema.Tuple([Schema.String, Schema.Array(Anchor)])),
});
export type FullTextAssignment = typeof FullTextAssignment.Type;

// kittum.extraction: not served yet (WP-11), so only the mock implements it.

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
  report: Schema.Struct({
    id: Id,
    study_id: Id,
    study_label: Schema.String,
    title: Schema.String,
    citation: Schema.String,
    licence: Schema.String,
  }),
  document: Document,
  layer: LayerSummary,
  anchors: Schema.Array(Anchor),
  fields: Schema.Array(ReviewField),
});
export type ExtractionReview = typeof ExtractionReview.Type;
