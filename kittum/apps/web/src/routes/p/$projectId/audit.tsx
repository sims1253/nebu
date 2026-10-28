import { createFileRoute } from "@tanstack/react-router";

import { AuditScreen } from "../../../screens/audit/Audit.tsx";

export const Route = createFileRoute("/p/$projectId/audit")({
  component: AuditScreen,
});
