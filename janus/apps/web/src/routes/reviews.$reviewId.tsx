import { createFileRoute } from "@tanstack/react-router";
import { ReviewPage } from "~/components/ReviewPage";

export const Route = createFileRoute("/reviews/$reviewId")({
  component: ReviewPage,
});
