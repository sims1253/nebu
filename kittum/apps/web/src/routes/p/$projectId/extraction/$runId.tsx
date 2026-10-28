import { createFileRoute } from "@tanstack/react-router";

import { run } from "../../../../data/workbench.ts";
import { ExtractionScreen } from "../../../../screens/extraction/ExtractionReview.tsx";

export const Route = createFileRoute("/p/$projectId/extraction/$runId")({
  loader: ({ context, params }) =>
    run(context.workbench.extraction({ projectId: params.projectId, runId: params.runId })),
  component: function ExtractionRoute() {
    const review = Route.useLoaderData();
    // A new run is a new review: no state carries over from the last one.
    return <ExtractionScreen key={review.run.id} review={review} />;
  },
});
