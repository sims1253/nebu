import { createFileRoute, Navigate } from "@tanstack/react-router";

import { useProject } from "../../../screens/project.tsx";

// Each person starts where their work is: screeners and auditors in their queue, adjudicators in
// theirs, the methods lead on the progress page.
export const Route = createFileRoute("/p/$projectId/")({
  component: function Start() {
    const { project, has, screeningStage } = useProject();
    const to = screeningStage("title_abstract")
      ? "/p/$projectId/screening/title-abstract"
      : has("adjudicator") && !has("lead")
        ? "/p/$projectId/adjudication"
        : has("lead")
          ? "/p/$projectId/progress"
          : "/p/$projectId/protocol";
    return <Navigate to={to} params={{ projectId: project.id }} replace />;
  },
});
