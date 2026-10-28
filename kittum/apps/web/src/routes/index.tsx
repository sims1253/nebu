import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { health } from "../data/api.ts";
import { run } from "../data/workbench.ts";
import { rememberReviewer } from "../screens/project.tsx";
import { failure, Field, FormDialog, TextInput } from "../ui/Form.tsx";
import { Key, KeyHint } from "../ui/Key.tsx";
import { useKeys } from "../ui/keys.ts";

export const Route = createFileRoute("/")({
  loader: ({ context }) => run(context.workbench.projects()),
  component: Projects,
});

function Projects() {
  const projects = Route.useLoaderData();
  const navigate = Route.useNavigate();
  const { workbench } = Route.useRouteContext();
  const [creating, setCreating] = useState(false);
  useKeys((event) => {
    if (event.key === "c" && workbench.live) {
      setCreating(true);
      return true;
    }
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
        <div className="mt-1 flex items-baseline gap-6">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">
            {projects.length ? "Choose a review" : "Start a review"}
          </h1>
          {workbench.live ? (
            <button
              type="button"
              onClick={() => setCreating(true)}
              className="ml-auto rounded-[3px] px-1 py-0.5 hover:bg-sheet"
            >
              <KeyHint keys={["c"]}>Create a review</KeyHint>
            </button>
          ) : null}
        </div>
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
            There are no reviews on this server yet. Create one with <Key k="c" />: you become its
            methods lead and set it up step by step, from the people to the first screening stage.
            For a complete example to explore, run <code>kittum demo seed</code>.
          </p>
        ) : null}
        <ServerStatus />
        <CreateReview
          open={creating}
          onOpenChange={setCreating}
          onCreated={(projectId, leadId) => {
            rememberReviewer(projectId, leadId);
            void navigate({ to: "/p/$projectId/setup", params: { projectId } });
          }}
        />
      </div>
    </div>
  );
}

function CreateReview({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (projectId: string, leadId: string) => void;
}) {
  const { workbench } = Route.useRouteContext();
  const [name, setName] = useState("");
  const [lead, setLead] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const create = async () => {
    if (!name.trim() || !lead.trim()) {
      setError("Name the review and its methods lead.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const project = await run(
        workbench.createProject({ name: name.trim(), leadName: lead.trim() }),
      );
      const [first] = project.members;
      if (!first) throw new Error("the review came back without its lead");
      onCreated(project.id, first.actor_id);
    } catch (e) {
      setError(failure(e, "The review was not created. Is the server running?"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Create a review"
      description="You are its methods lead: you add the people, publish the protocol, run the searches, and open screening. Each step goes into the review's ledger."
      submit="Create the review"
      busy={busy}
      error={error}
      onSubmit={() => void create()}
    >
      <Field label="Review name" hint="A short name the team knows it by.">
        <TextInput
          autoFocus
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
          placeholder="Nicotine replacement update 2026"
        />
      </Field>
      <Field label="Your name">
        <TextInput
          value={lead}
          onChange={(e) => setLead(e.currentTarget.value)}
          placeholder="Morgan Lee"
        />
      </Field>
    </FormDialog>
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
