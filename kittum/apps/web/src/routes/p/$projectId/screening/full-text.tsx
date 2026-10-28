import { createFileRoute } from "@tanstack/react-router";

import { run } from "../../../../data/workbench.ts";
import { FullTextScreen } from "../../../../screens/screening/FullText.tsx";

export const Route = createFileRoute("/p/$projectId/screening/full-text")({
  loader: ({ context, params }) => run(context.workbench.fullText(params.projectId)),
  component: function FullTextRoute() {
    return <FullTextScreen assignment={Route.useLoaderData()} />;
  },
});
