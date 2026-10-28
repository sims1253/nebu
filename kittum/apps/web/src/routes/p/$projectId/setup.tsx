import { createFileRoute } from "@tanstack/react-router";

import { SetupScreen } from "../../../screens/setup/Setup.tsx";

export const Route = createFileRoute("/p/$projectId/setup")({ component: SetupScreen });
