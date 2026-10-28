import { createFileRoute } from "@tanstack/react-router";

import { TitleAbstractQueue } from "../../../../screens/screening/TitleAbstract.tsx";

export const Route = createFileRoute("/p/$projectId/screening/title-abstract")({
  component: TitleAbstractQueue,
});
