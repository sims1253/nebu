import { createRootRoute, Outlet } from "@tanstack/react-router";
import { AppShell } from "~/components/AppShell";
import { Toaster } from "~/components/ui/toast";

function RootComponent() {
  return (
    <AppShell>
      <Outlet />
      <Toaster />
    </AppShell>
  );
}

export const Route = createRootRoute({
  component: RootComponent,
});
