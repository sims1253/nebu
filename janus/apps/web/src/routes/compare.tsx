import { createFileRoute } from "@tanstack/react-router";
import { UploadView } from "~/components/UploadView";
import { ReviewList } from "~/components/ReviewList";

function ComparisonPage() {
  return (
    <>
      <UploadView />
      <ReviewList />
    </>
  );
}
export const Route = createFileRoute("/compare")({ component: ComparisonPage });
