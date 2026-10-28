import { createFileRoute } from "@tanstack/react-router";

import { ExtractionRunsScreen } from "../../../../screens/extraction/ExtractionRuns.tsx";

export const Route = createFileRoute("/p/$projectId/extraction/")({
  component: ExtractionRunsScreen,
});
