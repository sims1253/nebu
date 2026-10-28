// The open project, its protocol, and who is at the workbench, for every screen inside it.
import { createContext, useContext } from "react";

import type {
  Criterion,
  Member,
  ProfileLabels,
  ProjectSummary,
  ProtocolVersion,
  Role,
  Stage,
  StageInfo,
} from "../data/schema.ts";

export interface ProjectState {
  readonly project: ProjectSummary;
  readonly projects: ReadonlyArray<ProjectSummary>;
  /** Null until a protocol is published. */
  readonly protocol: ProtocolVersion | null;
  /** The person acting. There is no sign-in yet: the reviewer picks who they are. */
  readonly me: Member;
}

export const ProjectContext = createContext<ProjectState | null>(null);

/** The core's words, for a project without a published protocol. */
const CORE_LABELS: ProfileLabels = {
  scope: "Question",
  population: "Population",
  intervention: "Intervention",
  comparator: "Comparator",
  outcome: "Outcome",
  study_type: "Study type",
  other: "Other",
  report: "Report",
  study: "Study",
  search_topic: "Search topic",
};

// Who acts is remembered per browser tab, so two reviewers can work side by side in two tabs.
const key = (projectId: string) => `kittum.reviewer.${projectId}`;

export const rememberedReviewer = (projectId: string): string | null =>
  sessionStorage.getItem(key(projectId)) ?? localStorage.getItem(key(projectId));

export const rememberReviewer = (projectId: string, actorId: string | null) => {
  for (const store of [sessionStorage, localStorage]) {
    if (actorId) store.setItem(key(projectId), actorId);
    else store.removeItem(key(projectId));
  }
};

/** An English plural for a label ("Study" to "Studies"). Profile labels are singular nouns. */
export const plural = (word: string) =>
  /[^aeiou]y$/i.test(word)
    ? `${word.slice(0, -1)}ies`
    : /(s|x|ch|sh)$/i.test(word)
      ? `${word}es`
      : `${word}s`;

/** Why the catalogue is the lead's alone, for screens that would show it. */
export const LEAD_ONLY =
  "Only the methods lead sees every record. A reviewer who could list them all could tell which records in their queue were placed there to check them.";

export const ROLE_LABELS: Record<Role, string> = {
  lead: "Methods lead",
  reviewer: "Reviewer",
  auditor: "Auditor",
  adjudicator: "Adjudicator",
};

export function useProject() {
  const state = useContext(ProjectContext);
  if (!state) throw new Error("useProject outside a project");
  return derive(state);
}

// Derived once per project state, so screens can depend on what it returns. The layout, which
// provides the context, derives from the state directly.
const derived = new WeakMap<ProjectState, ReturnType<typeof compute>>();
export const derive = (state: ProjectState) => {
  let found = derived.get(state);
  if (!found) derived.set(state, (found = compute(state)));
  return found;
};

function compute(state: ProjectState) {
  const labels = state.protocol?.profile.labels ?? CORE_LABELS;
  const topics = state.protocol?.protocol.topics ?? [];
  const scopes = topics.flatMap((t) => t.scopes);
  const me = state.me;
  const has = (role: Role) => me.roles.includes(role);
  /** The stage of a kind that `me` screens: as a screener, or as its auditor. */
  const screeningStage = (stage: Stage): StageInfo | null =>
    state.project.stages.find(
      (s) =>
        s.stage === stage && (s.screeners.includes(me.actor_id) || s.auditor_id === me.actor_id),
    ) ?? null;
  /** The server's rule: a lead who screens, audits, or adjudicates no stage sees what only
   * the lead sees (every record, the stages' figures, the reports). */
  const seesLeadData =
    has("lead") &&
    !state.project.stages.some(
      (s) =>
        s.screeners.includes(me.actor_id) ||
        s.auditor_id === me.actor_id ||
        s.adjudicators.includes(me.actor_id),
    );
  const adjudicatedStages = state.project.stages.filter((s) =>
    s.adjudicators.includes(me.actor_id),
  );
  const scopeLabel = (scopeId: string) => scopes.find((s) => s.id === scopeId)?.label ?? scopeId;
  /** The criteria decided at `stage` in a scope, in key order. */
  const criteriaFor = (stage: Stage, scopeId: string | null): Criterion[] =>
    topics
      .flatMap((t) => t.criteria)
      .filter(
        (c) => c.decidable_at.includes(stage) && (!scopeId || c.applies_to.includes(scopeId)),
      );
  return {
    ...state,
    labels,
    scopes,
    has,
    seesLeadData,
    screeningStage,
    adjudicatedStages,
    scopeLabel,
    criteriaFor,
  };
}
