import { createFileRoute } from "@tanstack/react-router";
import { UploadView } from "~/components/UploadView";
import { ReviewList } from "~/components/ReviewList";

function IndexPage() {
  return (
    <div className="space-y-8">
      <UploadView />
      <ReviewList />
    </div>
  );
}

export const Route = createFileRoute("/")({
  component: IndexPage,
});
