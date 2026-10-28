// The one interface the screens use for data. `api.ts` implements it on the generated client in
// @kittum/contracts; `mock/` implements it on recorded fixtures for the unit tests. Calls that
// depend on who asks take `as`, the reviewer's actor ID, which the API sends as
// `X-Kittum-Reviewer` (a stand-in until the API has authentication).
import * as Data from "effect/Data";
import * as Effect from "effect/Effect";

import type {
  Acceptance,
  AdjudicationItem,
  AdjudicationResult,
  Anchor,
  AsOf,
  ChainAudit,
  CheckpointStatus,
  EventPage,
  Milestone,
  RecordTrace,
  Box,
  CorpusRevision,
  CorpusView,
  DecideResult,
  Decision,
  DecisionTiming,
  Assertion,
  DiffPreview,
  ExampleProtocol,
  ExtractionReview,
  ExtractionRun,
  ExtractionSchema,
  FullText,
  FullTextAssignment,
  IdentityRevision,
  LayerSummary,
  LedgerSummary,
  LinkProposal,
  Member,
  PageLayer,
  ParsedSource,
  Phase,
  Progress,
  ProjectSummary,
  ProtocolSource,
  RenderedReport,
  ReportInfo,
  RetrievalItem,
  RunListing,
  RevealSetting,
  Role,
  SealedBundle,
  SearchPlan,
  Sentinel,
  StoredProtocol,
  ProtocolVersion,
  QueueItem,
  ScopeStatus,
  SearchExecution,
  Stage,
  StageInfo,
  ValidationReport,
} from "./schema.ts";

export class WorkbenchError extends Data.TaggedError("WorkbenchError")<{
  readonly reason: "not_found" | "forbidden" | "conflict" | "invalid" | "unavailable";
  readonly message: string;
}> {}

type Call<A> = Effect.Effect<A, WorkbenchError>;

/** An edited extraction value with its source span: code-point offsets and per-line boxes. */
interface AcceptedEdit {
  readonly value: string;
  readonly start: number;
  readonly end: number;
  readonly quote: string;
  readonly boxes: ReadonlyArray<Box>;
}

/** Who asks, in which project. */
export interface Caller {
  readonly projectId: string;
  readonly as: string;
}

interface StageRef extends Caller {
  readonly scopeId: string;
  readonly stage: Stage;
}

export interface Workbench {
  /** False for the mock: the screens say they show example data. */
  readonly live: boolean;

  readonly projects: () => Call<ReadonlyArray<ProjectSummary>>;
  /** The published protocol, or null before one is published. */
  readonly protocol: (projectId: string) => Call<ProtocolVersion | null>;

  // Setting a review up. Each step is the methods lead's; the server records it in the ledger.

  /** A new review and its methods lead, who adds everyone else. */
  readonly createProject: (request: {
    readonly name: string;
    readonly leadName: string;
  }) => Call<ProjectSummary>;
  readonly addMember: (
    request: Caller & { readonly displayName: string; readonly roles: ReadonlyArray<Role> },
  ) => Call<Member>;
  /** The example protocols the server ships, to start a review from. */
  readonly protocolExamples: () => Call<ReadonlyArray<ExampleProtocol>>;
  /** The published protocol as YAML, to edit into the next version. */
  readonly protocolSource: (projectId: string) => Call<string>;
  /** YAML or JSON text as data; a syntax error comes back as an issue. */
  readonly parseProtocol: (text: string) => Call<ParsedSource>;
  readonly validateProtocol: (source: ProtocolSource) => Call<ValidationReport>;
  /** What publishing `source` would change against the published version; saves nothing. */
  readonly previewProtocol: (
    request: Caller & { readonly source: ProtocolSource },
  ) => Call<DiffPreview>;
  /** Saves `source` as a draft and publishes it with the caller's approval. */
  readonly publishProtocol: (
    request: Caller & { readonly source: ProtocolSource; readonly reason: string | null },
  ) => Call<StoredProtocol>;
  /** A live search (PubMed or ClinicalTrials.gov) through the server's gateway. */
  readonly runSearch: (request: Caller & { readonly plan: SearchPlan }) => Call<SearchExecution>;
  /** An export file: RIS, Cochrane, Embase, PubMed XML, or a registry's CSV. */
  readonly importSearch: (
    request: Caller & { readonly plan: SearchPlan; readonly data: string; readonly format: string },
  ) => Call<SearchExecution>;
  /** Deduplicates every search into the first identity revision. */
  readonly ingestCorpus: (caller: Caller) => Call<CorpusRevision>;
  /** A sentinel file as `kittum project open-stage --sentinels` reads it. */
  readonly parseSentinels: (
    request: Caller & { readonly text: string },
  ) => Call<ReadonlyArray<Sentinel>>;
  /** Every report of the corpus with its outcomes and full text: the lead's upload queue. */
  readonly fullTexts: (caller: Caller) => Call<ReadonlyArray<RetrievalItem>>;
  readonly attachFullText: (
    request: Caller & {
      readonly itemRef: string;
      readonly pdf: Uint8Array;
      readonly licence: string | null;
    },
  ) => Call<FullText>;
  /** A report with a known outcome, which full-text stages seed into every queue. */
  readonly attachSentinelFullText: (
    request: Caller & {
      readonly title: string;
      readonly expectedOutcome: "include" | "exclude";
      readonly pdf: Uint8Array;
      readonly licence: string | null;
    },
  ) => Call<FullText>;

  /** The reviewer's next undecided assignments in queue order, after `after`. */
  readonly queue: (
    request: StageRef & { readonly after: string | null; readonly limit: number },
  ) => Call<ReadonlyArray<QueueItem>>;

  /**
   * A provisional decision returns `phase: "final"` when no reveal follows (the AI agrees, or the
   * stage never reveals), and `phase: "provisional"` with the reveal otherwise. The reviewer then
   * keeps or revises with `phase: "final"`. `expectedEtag` is the token the item last came with.
   */
  readonly decide: (
    request: Caller & {
      readonly assignmentId: string;
      readonly decision: Decision;
      readonly phase: Phase;
      readonly expectedEtag: string;
      readonly timing: DecisionTiming;
    },
  ) => Call<DecideResult>;

  /** Takes a decision back; the item returns to the queue under the returned token. */
  readonly undo: (
    request: Caller & { readonly assignmentId: string; readonly expectedEtag: string },
  ) => Call<{ readonly etag: string }>;

  readonly status: (request: StageRef) => Call<ScopeStatus>;

  /** The reviewer's next full text in the scope, or null when none is waiting. */
  readonly fullText: (
    request: Caller & { readonly scopeId: string },
  ) => Call<FullTextAssignment | null>;
  readonly page: (layer: LayerSummary, page: number) => Call<PageLayer>;
  /** Saves a mention or a selection (code-point offsets into the layer) as an evidence anchor. */
  readonly adopt: (request: {
    readonly layerId: string;
    readonly quote: string;
    readonly start: number;
  }) => Call<Anchor>;

  readonly adjudications: (request: StageRef) => Call<ReadonlyArray<AdjudicationItem>>;
  readonly adjudicate: (
    request: Caller & {
      readonly conflictId: string;
      readonly expectedEtag: string;
      /** A column label to adopt, or null with an own decision. */
      readonly adopted: string | null;
      readonly decision: Decision | null;
      readonly timing: DecisionTiming;
    },
  ) => Call<AdjudicationResult>;

  readonly progress: (caller: Caller) => Call<Progress>;
  /** The methods lead opens a scope at a stage; the AI judge starts reading at once. */
  readonly openStage: (
    request: Caller & {
      readonly scopeId: string;
      readonly stage: Stage;
      readonly reveal?: RevealSetting;
      readonly sentinels?: ReadonlyArray<Sentinel>;
    },
  ) => Call<StageInfo>;
  readonly ledger: (projectId: string) => Call<LedgerSummary>;

  // The catalogue (every search's records, the corpus, the link proposals) is the methods
  // lead's alone: a reviewer who could list every record could tell the sentinels in their
  // queue by their absence. The server refuses anyone else (`not_found` is not used for it).
  readonly searches: (caller: Caller) => Call<ReadonlyArray<SearchExecution>>;
  /** The newest identity revision with its reports, or null before the corpus is ingested. */
  readonly corpus: (caller: Caller) => Call<CorpusView | null>;
  readonly proposals: (caller: Caller) => Call<ReadonlyArray<LinkProposal>>;
  readonly link: (
    request: Caller & { readonly revisionId: string } & (
        | { readonly action: "confirm"; readonly proposalId: string }
        | {
            readonly action: "merge";
            readonly occurrenceIds: ReadonlyArray<string>;
            readonly relation: "same_record" | "same_report" | "same_study";
          }
        | {
            readonly action: "split";
            readonly occurrenceIds: ReadonlyArray<string>;
            readonly level: "report" | "study";
          }
      ),
  ) => Call<IdentityRevision>;

  /** Reports, the methods lead's alone: render a target from the ledger, fetch one of its
   * files, and seal the handover bundle. */
  readonly renderReport: (
    request: Caller & {
      readonly target: string;
      /** The ledger event to render at; the head when absent. */
      readonly cutoffSeq?: number;
    },
  ) => Call<RenderedReport>;
  readonly reportFile: (request: Caller & { readonly sha256: string }) => Call<Uint8Array>;
  readonly sealBundle: (caller: Caller) => Call<SealedBundle>;

  // The audit trail, the methods lead's alone: the ledger holds every reviewer's decisions.

  /** Every ledger event that concerns a record: a report, a search result, or a study. */
  readonly trace: (request: Caller & { readonly ref: string }) => Call<RecordTrace>;
  /** Ledger events after `afterSeq`, oldest first. */
  readonly ledgerEvents: (
    request: Caller & { readonly afterSeq: number; readonly limit: number },
  ) => Call<EventPage>;
  /** Every check on every event and checkpoint, naming each one that fails. */
  readonly auditLedger: (caller: Caller) => Call<ChainAudit>;
  /** Signs the chain head with the server's checkpoint key. */
  readonly signCheckpoint: (caller: Caller) => Call<CheckpointStatus>;
  readonly milestones: (caller: Caller) => Call<ReadonlyArray<Milestone>>;
  /** The PRISMA flows and the AI disclosure at a cutoff (the head when null). */
  readonly asOf: (request: Caller & { readonly cutoffSeq: number | null }) => Call<AsOf>;
  /** An artifact a ledger event names: a raw response, a full text, a model's raw output. */
  readonly auditArtifact: (request: Caller & { readonly sha256: string }) => Call<Uint8Array>;

  // Extraction. The lead requests runs on reports with a full text (`fullTexts` names them); a
  // worker fills each run; a reviewer accepts each value after viewing its source.

  // A run exists only for a report included at full text, so the server shows a run only to a
  // lead who screens nothing or to someone who has decided every screen of its report.
  readonly extractionRuns: (caller: Caller) => Call<ReadonlyArray<RunListing>>;
  /** The model recipes the LLM path can use here; empty means the Janus path only. */
  readonly extractionModels: (projectId: string) => Call<ReadonlyArray<string>>;
  /** An extraction schema file (YAML or JSON) as the server reads it. */
  readonly parseExtractionSchema: (
    request: Caller & { readonly text: string },
  ) => Call<ExtractionSchema>;
  readonly requestExtraction: (
    request: Caller & {
      readonly layerId: string;
      readonly studyId: string;
      readonly reportId: string;
      readonly schema: ExtractionSchema;
      /** The model for the LLM path, or null for the Janus path only. */
      readonly modelId: string | null;
      readonly report: ReportInfo;
    },
  ) => Call<ExtractionRun>;
  readonly extraction: (request: Caller & { readonly runId: string }) => Call<ExtractionReview>;
  readonly accept: (
    request: Caller & {
      readonly fieldId: string;
      readonly path: "janus" | "llm";
      /** The reviewer's own value and the span it came from, when they edited the path's value. */
      readonly edit: AcceptedEdit | null;
      /** How long the source region was on screen before the reviewer accepted. */
      readonly sourceViewedMs: number;
      /** The reviewer checked the source although a validator failed on the value. */
      readonly override: boolean;
    },
  ) => Call<Acceptance>;
  /** Agreeing, validated standard fields together, after the list was on screen. */
  readonly groupAccept: (
    request: Caller & {
      readonly runId: string;
      readonly fieldIds: ReadonlyArray<string>;
      readonly listViewedMs: number;
    },
  ) => Call<ReadonlyArray<Acceptance>>;
  /** The accepted values of the project, the latest per semantic key: what reports use. */
  readonly assertions: (caller: Caller) => Call<ReadonlyArray<Assertion>>;
}

export const run = <A>(call: Call<A>): Promise<A> => Effect.runPromise(call);
