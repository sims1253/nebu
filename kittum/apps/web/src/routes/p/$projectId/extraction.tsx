import { createFileRoute } from "@tanstack/react-router";

import { run } from "../../../data/workbench.ts";
import { ExtractionScreen } from "../../../screens/extraction/ExtractionReview.tsx";

export const Route = createFileRoute("/p/$projectId/extraction")({
  loader: ({ context, params }) => run(context.workbench.extraction(params.projectId)),
  component: function ExtractionRoute() {
    return <ExtractionScreen review={Route.useLoaderData()} />;
  },
});
