// The open project and its protocol, for every screen inside it.
import { createContext, useContext } from "react";

import type {
  Criterion,
  ProfileLabels,
  ProjectSummary,
  ProtocolVersion,
  Scope,
  Stage,
} from "../data/schema.ts";

export interface ProjectState {
  readonly project: ProjectSummary;
  readonly projects: ReadonlyArray<ProjectSummary>;
  readonly protocol: ProtocolVersion;
}

export const ProjectContext = createContext<ProjectState | null>(null);

export function useProject() {
  const state = useContext(ProjectContext);
  if (!state) throw new Error("useProject outside a project");
  const labels: ProfileLabels = state.protocol.profile.labels;
  const scopes: Scope[] = state.protocol.protocol.topics.flatMap((t) => t.scopes);
  const scope = scopes.find((s) => s.id === state.project.scope_id) ?? null;
  /** The criteria a reviewer decides at `stage` in the current scope, in key order. */
  const criteriaFor = (stage: Stage): Criterion[] =>
    state.protocol.protocol.topics
      .flatMap((t) => t.criteria)
      .filter((c) => c.decidable_at.includes(stage) && (!scope || c.applies_to.includes(scope.id)));
  return { ...state, labels, scope, criteriaFor };
}
