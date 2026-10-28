import { createFileRoute } from "@tanstack/react-router";

import { Placeholder } from "../../../screens/Placeholder.tsx";

export const Route = createFileRoute("/p/$projectId/studies")({
  component: () => (
    <Placeholder
      title="Studies"
      shows={() => [
        "Proposed clusters of records that report the same study",
        "The linking evidence: a shared registry number, a DOI family, or title similarity",
      ]}
      keys={[
        [["Enter"], "Confirm"],
        [["s"], "Split"],
        [["m"], "Merge"],
      ]}
      waitsOn="Waits on WP-07 (corpus identities) and its API routes."
    />
  ),
});
