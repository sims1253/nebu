import { createFileRoute } from "@tanstack/react-router";

import { StudiesScreen } from "../../../screens/studies/Studies.tsx";

export const Route = createFileRoute("/p/$projectId/studies")({
  component: StudiesScreen,
});
