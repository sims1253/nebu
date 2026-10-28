import { createFileRoute } from "@tanstack/react-router";

import { ReportsScreen } from "../../../screens/reports/Reports.tsx";

export const Route = createFileRoute("/p/$projectId/reports")({
  component: ReportsScreen,
});
