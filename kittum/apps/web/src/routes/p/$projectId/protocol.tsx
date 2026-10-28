import { createFileRoute } from "@tanstack/react-router";

import { ProtocolScreen } from "../../../screens/protocol/Protocol.tsx";

export const Route = createFileRoute("/p/$projectId/protocol")({
  component: ProtocolScreen,
});
