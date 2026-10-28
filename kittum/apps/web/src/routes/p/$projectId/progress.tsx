import { createFileRoute } from "@tanstack/react-router";

import { Placeholder } from "../../../screens/Placeholder.tsx";

export const Route = createFileRoute("/p/$projectId/progress")({
  component: () => (
    <Placeholder
      title="Progress"
      shows={(labels) => [
        `The live PRISMA flow for each ${labels.scope.toLowerCase()}, with the update-search delta`,
        "Throughput, kappa and PABAK for human pairs and human-AI pairs, and the conflict rate",
        "The AI-exclusion audit gauge and the AI disclosure summary",
        "Your own session metrics; never another reviewer's",
      ]}
      keys={[]}
      waitsOn="Waits on WP-09 (screening status), WP-13 (statistics), and their API routes."
    />
  ),
});
