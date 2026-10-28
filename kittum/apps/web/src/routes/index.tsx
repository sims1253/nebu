import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { health } from "../data/api.ts";
import { run } from "../data/workbench.ts";
import { Key } from "../ui/Key.tsx";
import { useKeys } from "../ui/keys.ts";

export const Route = createFileRoute("/")({
  loader: ({ context }) => run(context.workbench.projects()),
  component: Projects,
});

function Projects() {
  const projects = Route.useLoaderData();
  const navigate = Route.useNavigate();
  useKeys((event) => {
    const index = Number(event.key) - 1;
    const project = projects[index];
    if (!project) return false;
    void navigate({ to: "/p/$projectId", params: { projectId: project.id } });
    return true;
  });
  return (
    <div className="min-h-full bg-paper">
      <div className="mx-auto max-w-[880px] px-8 pt-20 pb-12">
        <p className="text-sm font-semibold text-petrol">Kittum</p>
        <h1 className="mt-1 text-xl font-semibold tracking-[-0.01em]">Choose a review</h1>
        <ol className="mt-8 border-y border-rule">
          {projects.map((project, index) => (
            <li key={project.id} className="border-b border-rule last:border-b-0">
              <Link
                to="/p/$projectId"
                params={{ projectId: project.id }}
                className="grid grid-cols-[28px_1fr_auto] items-baseline gap-x-4 px-2 py-3.5 hover:bg-sheet focus-visible:bg-sheet"
              >
                <Key k={String(index + 1)} />
                <span>
                  <span className="text-md font-semibold">{project.name}</span>
                  <span className="ml-3 text-ink-2">
                    {project.title ?? "No protocol published yet"}
                  </span>
                </span>
                <span className="text-xs text-ink-3">
                  {project.stages.length === 0
                    ? "Not screening yet"
                    : `${project.stages.length} ${project.stages.length === 1 ? "stage" : "stages"} open`}
                </span>
              </Link>
            </li>
          ))}
        </ol>
        {projects.length === 0 ? (
          <p className="mt-6 max-w-[60ch] text-ink-2">
            There are no reviews on this server yet. Run <code>kittum demo seed</code> for a
            complete example, or create one with <code>kittum project create</code>.
          </p>
        ) : null}
        <ServerStatus />
      </div>
    </div>
  );
}

function ServerStatus() {
  const { workbench } = Route.useRouteContext();
  const [status, setStatus] = useState<string | null>(
    workbench.live ? null : "Example data: no server behind these screens.",
  );
  useEffect(() => {
    if (!workbench.live) return;
    let live = true;
    // A proxy to a server that is not running can hang; give up after three seconds.
    const timeout = new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error("timeout")), 3000),
    );
    Promise.race([health(), timeout]).then(
      (health) => live && setStatus(`Server ${health.status}, version ${health.version}.`),
      () => live && setStatus("The server is not reachable. Start it with `kittum serve`."),
    );
    return () => {
      live = false;
    };
  }, [workbench]);
  return (
    <footer className="mt-10 flex gap-4 text-xs text-ink-3">
      <span>{status ?? "Checking the server."}</span>
      <span>
        AGPL-3.0-or-later.{" "}
        <a className="underline" href="https://github.com/sims1253/nebu/tree/main/kittum">
          Source
        </a>
      </span>
    </footer>
  );
}
