import { createRootRoute, Outlet } from "@tanstack/react-router";

export const Route = createRootRoute({
  component: () => (
    <div className="min-h-screen bg-stone-50 text-stone-900">
      <header className="border-b border-stone-200 px-6 py-3 font-semibold">Kittum</header>
      <main className="px-6 py-6">
        <Outlet />
      </main>
      <footer className="px-6 py-3 text-xs text-stone-500">
        Licensed under the AGPL-3.0-or-later.{" "}
        <a className="underline" href="https://github.com/sims1253/nebu/tree/main/kittum">
          Source
        </a>
      </footer>
    </div>
  ),
});
