import { createFileRoute } from "@tanstack/react-router";

import { ProgressScreen } from "../../../screens/progress/Progress.tsx";

export const Route = createFileRoute("/p/$projectId/progress")({
  component: ProgressScreen,
});
