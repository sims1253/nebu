import { createFileRoute } from "@tanstack/react-router";

import { Placeholder } from "../../../screens/Placeholder.tsx";

export const Route = createFileRoute("/p/$projectId/reports")({
  component: () => (
    <Placeholder
      title="Reports"
      shows={() => [
        "The PRISMA 2020 flow diagram from a frozen identity revision",
        "Exports: tables, included and excluded records with reasons, and RIS files per database",
        "The handover bundle: the ledger, artifacts, and the verify tool, signed",
      ]}
      keys={[]}
      waitsOn="Waits on WP-14 (reports) and its API routes. A review profile can add its own report templates."
    />
  ),
});
