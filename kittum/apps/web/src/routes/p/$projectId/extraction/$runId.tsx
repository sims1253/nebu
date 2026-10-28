import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import type { ExtractionReview } from "../../../../data/schema.ts";
import { run } from "../../../../data/workbench.ts";
import { ExtractionScreen } from "../../../../screens/extraction/ExtractionReview.tsx";
import { useProject } from "../../../../screens/project.tsx";
import { failure } from "../../../../ui/Form.tsx";

export const Route = createFileRoute("/p/$projectId/extraction/$runId")({
  component: function ExtractionRoute() {
    const { workbench } = Route.useRouteContext();
    const { runId } = Route.useParams();
    // Loaded as the person at the workbench: the server shows a run only to whoever may see it.
    const { project, me } = useProject();
    const [review, setReview] = useState<ExtractionReview | null>(null);
    const [error, setError] = useState<string | null>(null);
    useEffect(() => {
      let live = true;
      setReview(null);
      run(workbench.extraction({ projectId: project.id, as: me.actor_id, runId })).then(
        (found) => live && setReview(found),
        (e: unknown) => live && setError(failure(e, "The run could not load.")),
      );
      return () => {
        live = false;
      };
    }, [workbench, project.id, me.actor_id, runId]);
    if (error) {
      return (
        <div className="mx-auto max-w-[640px] px-10 py-16">
          <p role="alert" className="text-md text-ink-2">
            {error}
          </p>
          <Link
            to="/p/$projectId/extraction"
            params={{ projectId: project.id }}
            className="mt-3 inline-block text-petrol hover:underline"
          >
            Runs
          </Link>
        </div>
      );
    }
    if (!review) return <p className="px-10 py-16 text-ink-3">Loading the run.</p>;
    // A new run is a new review: no state carries over from the last one.
    return <ExtractionScreen key={review.run.id} review={review} />;
  },
});
