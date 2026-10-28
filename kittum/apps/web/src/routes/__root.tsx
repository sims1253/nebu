import { createRootRouteWithContext, Outlet } from "@tanstack/react-router";

import type { Workbench } from "../data/workbench.ts";

export const Route = createRootRouteWithContext<{ workbench: Workbench }>()({
  component: Outlet,
  errorComponent: ({ error }) => (
    <div role="alert" className="mx-auto max-w-[60ch] px-6 py-16">
      <h1 className="text-lg font-semibold">The workbench could not load this screen</h1>
      <p className="mt-2 text-ink-2">{error instanceof Error ? error.message : String(error)}</p>
      <p className="mt-2 text-ink-2">Reload the page. If it fails again, the server may be down.</p>
    </div>
  ),
});
