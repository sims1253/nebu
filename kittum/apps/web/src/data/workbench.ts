// The one interface the screens use for data. `mock/` implements it today; an implementation on
// the generated client in @kittum/contracts replaces it once the routes exist (WP-15).
import * as Data from "effect/Data";
import * as Effect from "effect/Effect";

import type {
  Decision,
  DecideResult,
  DecisionTiming,
  ExtractionReview,
  FullTextAssignment,
  PageLayer,
  Phase,
  ProjectSummary,
  ProtocolVersion,
  QueueItem,
  ScopeStatus,
  Stage,
} from "./schema.ts";

export class WorkbenchError extends Data.TaggedError("WorkbenchError")<{
  readonly reason: "not_found" | "conflict" | "unavailable";
  readonly message: string;
}> {}

type Call<A> = Effect.Effect<A, WorkbenchError>;

export interface Workbench {
  readonly projects: () => Call<ReadonlyArray<ProjectSummary>>;
  readonly protocol: (projectId: string) => Call<ProtocolVersion>;

  /**
   * The reviewer's next assignments in queue order, after `after` (or from the start). The
   * workbench keeps five ahead of the current record, so the server must lease a batch, not one
   * record at a time as `ScreeningService.next` does now.
   */
  readonly queue: (request: {
    readonly scopeId: string;
    readonly stage: Stage;
    readonly after: string | null;
    readonly limit: number;
  }) => Call<ReadonlyArray<QueueItem>>;

  /**
   * A provisional decision returns `phase: "final"` when no reveal follows (the AI agrees, or the
   * project never reveals), and `phase: "provisional"` with the reveal otherwise. The reviewer then
   * keeps or revises with `phase: "final"`.
   */
  readonly decide: (request: {
    readonly assignmentId: string;
    readonly decision: Decision;
    readonly phase: Phase;
    readonly expectedRevision: number;
    readonly timing: DecisionTiming;
  }) => Call<DecideResult & { readonly revision: number }>;

  readonly status: (scopeId: string, stage: Stage) => Call<ScopeStatus>;

  readonly fullText: (projectId: string) => Call<FullTextAssignment | null>;
  readonly page: (layerId: string, page: number) => Call<PageLayer>;

  readonly extraction: (projectId: string) => Call<ExtractionReview | null>;
  readonly accept: (request: {
    readonly fieldId: string;
    readonly path: "janus" | "llm";
    /** How long the source region was on screen before the reviewer accepted. */
    readonly sourceViewedMs: number;
  }) => Call<void>;
}

export const run = <A>(call: Call<A>): Promise<A> => Effect.runPromise(call);
