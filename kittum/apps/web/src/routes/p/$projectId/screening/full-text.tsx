import { createFileRoute } from "@tanstack/react-router";

import { FullTextQueue } from "../../../../screens/screening/FullText.tsx";

export const Route = createFileRoute("/p/$projectId/screening/full-text")({
  component: FullTextQueue,
});
