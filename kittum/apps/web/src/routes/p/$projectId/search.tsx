import { createFileRoute } from "@tanstack/react-router";

import { Placeholder } from "../../../screens/Placeholder.tsx";

export const Route = createFileRoute("/p/$projectId/search")({
  component: () => (
    <Placeholder
      title="Search"
      shows={() => [
        "One tab per database with the line-numbered strategy, limits, date, and hit count",
        "The translation of each strategy against the primary database",
        "The peer-review checklist for the strategy",
        "Which known relevant records the strategy finds and which it misses",
      ]}
      keys={[
        [["a"], "Approve"],
        [["r"], "Request changes"],
      ]}
      waitsOn="Waits on WP-05 (search executions) and its API routes."
    />
  ),
});
