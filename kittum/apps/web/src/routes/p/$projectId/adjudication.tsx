import { createFileRoute } from "@tanstack/react-router";

import { AdjudicationQueue } from "../../../screens/adjudication/Adjudication.tsx";

export const Route = createFileRoute("/p/$projectId/adjudication")({
  component: AdjudicationQueue,
});
