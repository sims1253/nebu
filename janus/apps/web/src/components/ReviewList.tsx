import { Link } from "@tanstack/react-router";
import { X } from "lucide-react";
import type { ReviewStatus } from "@janus/contracts";
import { useDeleteReview, useReviews } from "~/lib/queries";

const labels: Record<ReviewStatus, string> = {
  created: "Created",
  validating_inputs: "Validating inputs",
  reading_document: "Reading document",
  extracting_fields: "Extracting fields",
  comparing: "Comparing",
  ready: "Ready",
  error: "Error",
};

export function ReviewList() {
  const reviews = useReviews();
  const remove = useDeleteReview();
  if (!reviews.data?.length) return null;
  return (
    <section className="mx-auto max-w-lg px-8 pb-8">
      <h2 className="mb-2 text-xs font-medium text-muted-foreground">
        Recent Reviews
      </h2>
      <div className="divide-y rounded border text-xs">
        {reviews.data.map((review) => (
          <div key={review.id} className="flex items-center hover:bg-muted/40">
            <Link
              className="flex min-w-0 flex-1 items-center justify-between gap-4 px-3 py-2"
              to="/reviews/$reviewId"
              params={{ reviewId: review.id }}
            >
              <span className="truncate">{review.document_filename}</span>
              <span
                className={
                  review.status === "error"
                    ? "text-destructive"
                    : "text-muted-foreground"
                }
              >
                {labels[review.status]}
              </span>
            </Link>
            <button
              className="mr-2 text-muted-foreground hover:text-destructive"
              aria-label={`Delete ${review.document_filename}`}
              onClick={() => remove.mutate(review.id)}
            >
              <X className="size-4" />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
