import { createFileRoute } from "@tanstack/react-router";

import { SearchScreen } from "../../../screens/search/Search.tsx";

export const Route = createFileRoute("/p/$projectId/search")({
  component: SearchScreen,
});
