import { createFileRoute } from "@tanstack/react-router";
import { ExtractionStart } from "~/components/ExtractionStart";

export const Route = createFileRoute("/")({ component: ExtractionStart });
