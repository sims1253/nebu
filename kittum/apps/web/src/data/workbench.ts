// The one interface the screens use for data. `api.ts` implements it on the generated client in
// @kittum/contracts; `mock/` implements it on recorded fixtures for the unit tests. Calls that
// depend on who asks take `as`, the reviewer's actor ID, which the API sends as
// `X-Kittum-Reviewer` (a stand-in until the API has authentication).
import * as Data from "effect/Data";
import * as Effect from "effect/Effect";

import type {
  AdjudicationItem,
  AdjudicationResult,
  Anchor,
  Box,
  CorpusView,
  DecideResult,
  Decision,
  DecisionTiming,
  ExtractionReview,
  FullTextAssignment,
  IdentityRevision,
  LayerSummary,
  LedgerSummary,
  LinkProposal,
  PageLayer,
  Phase,
  Progress,
  ProjectSummary,
  RenderedReport,
  SealedBundle,
  ProtocolVersion,
  QueueItem,
  ScopeStatus,
  SearchExecution,
  Stage,
  StageInfo,
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
    request: Caller & { readonly scopeId: string; readonly stage: Stage },
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
  readonly renderReport: (request: Caller & { readonly target: string }) => Call<RenderedReport>;
  readonly reportFile: (request: Caller & { readonly sha256: string }) => Call<Uint8Array>;
  readonly sealBundle: (caller: Caller) => Call<SealedBundle>;

  /** Extraction review; null until there is something to review. Not served yet (WP-11). */
  readonly extraction: (projectId: string) => Call<ExtractionReview | null>;
  readonly accept: (request: {
    readonly fieldId: string;
    readonly path: "janus" | "llm";
    /** The reviewer's own value and the span it came from, when they edited the path's value. */
    readonly edit: AcceptedEdit | null;
    /** How long the source region was on screen before the reviewer accepted. */
    readonly sourceViewedMs: number;
  }) => Call<void>;
}

export const run = <A>(call: Call<A>): Promise<A> => Effect.runPromise(call);
