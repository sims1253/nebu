import { createRouter } from "@tanstack/react-router";
import type { RouterHistory } from "@tanstack/react-router";

import type { Workbench } from "./data/workbench.ts";
import { routeTree } from "./routeTree.gen.ts";

export const createAppRouter = (workbench: Workbench, history?: RouterHistory) =>
  createRouter({
    routeTree,
    context: { workbench },
    defaultPreload: "intent",
    ...(history ? { history } : {}),
  });

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
