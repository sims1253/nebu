import { createFileRoute } from "@tanstack/react-router";

import { Placeholder } from "../../../screens/Placeholder.tsx";

export const Route = createFileRoute("/p/$projectId/protocol")({
  component: () => (
    <Placeholder
      title="Protocol"
      shows={(labels) => [
        `Review questions with ${[
          labels.population,
          labels.intervention,
          labels.comparator,
          labels.outcome,
        ]
          .map((label) => label.toLowerCase())
          .join(", ")} blocks, and the criteria table`,
        "The difference between two protocol versions, and the protocol hash",
        "A test bench that runs the AI judge on calibration records and shows which criteria it misreads",
        "Publish, which freezes and signs a version",
      ]}
      keys={[
        [["n"], "New criterion"],
        [["Ctrl", "Enter"], "Run the test bench"],
      ]}
      waitsOn="Waits on WP-03 (protocol compile and diff) and its API routes."
    />
  ),
});
