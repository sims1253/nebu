import { createFileRoute } from "@tanstack/react-router";

import { Placeholder } from "../../../screens/Placeholder.tsx";

export const Route = createFileRoute("/p/$projectId/risk-of-bias")({
  component: () => (
    <Placeholder
      title="Risk of bias"
      shows={() => [
        "Signalling questions with the quotes assembled for each, beside the report",
        "The study-level and outcome-level judgements, rendered as RoB 2 or the profile's form",
      ]}
      keys={[
        [["1", "5"], "Yes, probably yes, probably no, no, no information"],
        [["o"], "Override with a reason"],
      ]}
      waitsOn="Waits on WP-12 (appraisal) and its API routes."
    />
  ),
});
