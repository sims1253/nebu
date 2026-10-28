import { createFileRoute } from "@tanstack/react-router";

import { run } from "../api.ts";

export const Route = createFileRoute("/")({
  loader: () => run((api) => api.getHealth(undefined)),
  component: Home,
});

function Home() {
  const health = Route.useLoaderData();
  return (
    <p>
      Server {health.status}, version {health.version}.
    </p>
  );
}
