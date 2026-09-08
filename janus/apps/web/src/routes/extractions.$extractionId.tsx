import { createFileRoute } from "@tanstack/react-router";
import { ExtractionPage } from "~/components/ExtractionPage";

export const Route = createFileRoute("/extractions/$extractionId")({
  component: ExtractionPage,
});
