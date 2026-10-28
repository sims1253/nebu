import { Dialog } from "@base-ui/react/dialog";
import { Menu } from "@base-ui/react/menu";
import { createFileRoute, Link, Outlet, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { run } from "../../../data/workbench.ts";
import { ProjectContext } from "../../../screens/project.tsx";
import type { ProjectState } from "../../../screens/project.tsx";
import { Key } from "../../../ui/Key.tsx";
import { useKeys } from "../../../ui/keys.ts";
import { useTheme } from "../../../ui/theme.ts";

export const Route = createFileRoute("/p/$projectId")({
  loader: async ({ context, params }): Promise<ProjectState> => {
    const [projects, protocol] = await Promise.all([
      run(context.workbench.projects()),
      run(context.workbench.protocol(params.projectId)),
    ]);
    const project = projects.find((p) => p.id === params.projectId);
    if (!project) throw new Error(`There is no review with the ID ${params.projectId}.`);
    return { project, projects, protocol };
  },
  component: ProjectLayout,
});

type Section =
  | "protocol"
  | "search"
  | "studies"
  | "screening/title-abstract"
  | "screening/full-text"
  | "extraction"
  | "risk-of-bias"
  | "progress"
  | "reports";

const NAV: ReadonlyArray<{ section: Section; label: string; chord: string; group?: string }> = [
  { section: "protocol", label: "Protocol", chord: "p" },
  { section: "search", label: "Search", chord: "s" },
  { section: "studies", label: "Studies", chord: "d" },
  {
    section: "screening/title-abstract",
    label: "Title and abstract",
    chord: "t",
    group: "Screening",
  },
  { section: "screening/full-text", label: "Full text", chord: "f", group: "Screening" },
  { section: "extraction", label: "Extraction", chord: "x" },
  { section: "risk-of-bias", label: "Risk of bias", chord: "b" },
  { section: "progress", label: "Progress", chord: "o" },
  { section: "reports", label: "Reports", chord: "r" },
];

const path = (section: Section) => `/p/$projectId/${section}` as const;

function ProjectLayout() {
  const state = Route.useLoaderData();
  const navigate = useNavigate();
  const [chord, setChord] = useState(false);
  const [help, setHelp] = useState(false);
  const { theme, toggle } = useTheme();

  useKeys((event) => {
    if (chord) {
      setChord(false);
      const target = NAV.find((n) => n.chord === event.key);
      if (target)
        void navigate({ to: path(target.section), params: { projectId: state.project.id } });
      return true;
    }
    if (event.key === "g") {
      setChord(true);
      return true;
    }
    if (event.key === "h") {
      setHelp(true);
      return true;
    }
    return false;
  });

  return (
    <ProjectContext value={state}>
      <div className="flex h-full flex-col">
        <header className="flex h-10 flex-none items-center gap-3 border-b border-rule bg-sheet px-3">
          <Link to="/" className="px-1 text-sm font-semibold tracking-[-0.01em] text-petrol">
            Kittum
          </Link>
          <ProjectSwitcher state={state} />
          <nav
            aria-label="Review stages"
            className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto"
          >
            {NAV.map((item, index) => (
              <span key={item.section} className="flex items-center">
                {item.group && NAV[index - 1]?.group !== item.group ? (
                  <span className="mr-1 ml-2 text-xs text-ink-3">{item.group}</span>
                ) : null}
                <Link
                  to={path(item.section)}
                  params={{ projectId: state.project.id }}
                  className="rounded-[3px] px-[7px] py-1 text-sm whitespace-nowrap text-ink-2 hover:text-ink data-[status=active]:bg-petrol-wash data-[status=active]:font-medium data-[status=active]:text-petrol"
                  title={`g then ${item.chord}`}
                >
                  {item.label}
                </Link>
              </span>
            ))}
          </nav>
          {chord ? (
            <span className="text-xs text-ink-3" aria-live="polite">
              <Key k="g" /> then a stage key
            </span>
          ) : null}
          <button
            type="button"
            onClick={toggle}
            className="rounded-[3px] px-2 py-1 text-sm text-ink-2 hover:text-ink"
            aria-label={theme === "dark" ? "Use light theme" : "Use dark theme"}
          >
            {theme === "dark" ? "Light" : "Dark"}
          </button>
          <KeysDialog open={help} onOpenChange={setHelp} />
        </header>
        <main className="min-h-0 flex-1">
          <Outlet />
        </main>
      </div>
    </ProjectContext>
  );
}

function ProjectSwitcher({ state }: { state: ProjectState }) {
  const navigate = useNavigate();
  return (
    <Menu.Root>
      <Menu.Trigger className="flex max-w-[360px] min-w-0 flex-none items-baseline gap-2 rounded-[3px] px-2 py-1 text-left hover:bg-sheet-2 data-[popup-open]:bg-sheet-2">
        <span className="text-sm font-semibold">{state.project.active_substance}</span>
        <span className="hidden truncate text-xs text-ink-3 min-[1520px]:inline">
          {state.project.indication}
        </span>
        <svg
          width="9"
          height="6"
          viewBox="0 0 9 6"
          aria-hidden="true"
          className="self-center text-ink-3"
        >
          <path d="M1 1l3.5 3.5L8 1" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner sideOffset={6} align="start" className="z-20">
          <Menu.Popup className="w-[460px] rounded-[4px] border border-rule-strong bg-sheet py-1 shadow-[0_8px_24px_rgb(0_0_0/0.14)] outline-none">
            {state.projects.map((project) => (
              <Menu.Item
                key={project.id}
                onClick={() =>
                  void navigate({
                    to: "/p/$projectId/screening/title-abstract",
                    params: { projectId: project.id },
                  })
                }
                className="grid cursor-default grid-cols-[1fr_auto] gap-x-3 px-3 py-2 outline-none data-[highlighted]:bg-petrol-wash"
              >
                <span className="text-sm font-semibold">{project.active_substance}</span>
                <span className="text-xs text-ink-3">
                  {project.id === state.project.id ? "Open" : ""}
                </span>
                <span className="col-span-2 text-xs text-ink-2">{project.indication}</span>
                <span className="col-span-2 text-xs text-ink-3">{project.review}</span>
              </Menu.Item>
            ))}
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

const KEYS: ReadonlyArray<{
  screen: string;
  keys: ReadonlyArray<readonly [ReadonlyArray<string>, string]>;
}> = [
  {
    screen: "Everywhere",
    keys: [
      [["g", "t"], "Go to a stage: p, s, d, t, f, x, b, o, r"],
      [["h"], "Show these keys"],
    ],
  },
  {
    screen: "Title and abstract",
    keys: [
      [["i"], "Include"],
      [["e", "1–9"], "Exclude on a criterion"],
      [["u"], "Unclear"],
      [["Backspace"], "Undo the last decision"],
      [[" "], "Keep your decision and go to the next record"],
    ],
  },
  {
    screen: "Full text",
    keys: [
      [["↑", "↓"], "Choose the active criterion"],
      [["[", "]"], "Previous or next mention"],
      [["Enter"], "Adopt the mention or selection as evidence"],
      [["y", "n", "?"], "Met, not met, unclear"],
      [["i", "e", "x"], "Include, exclude, insufficient information"],
    ],
  },
  {
    screen: "Extraction",
    keys: [
      [["Tab"], "Next field; Shift+Tab goes back"],
      [["Enter"], "Accept, once the source was on screen"],
      [["1", "2"], "Pick a path when they disagree"],
      [["e"], "Edit from a selection on the page"],
    ],
  },
];

function KeysDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Trigger className="rounded-[3px] px-2 py-1 text-sm text-ink-2 hover:text-ink">
        Keys <Key k="h" />
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 bg-[rgb(10_20_24/0.32)]" />
        <Dialog.Popup className="fixed top-16 left-1/2 w-[560px] -translate-x-1/2 rounded-[4px] border border-rule-strong bg-sheet p-6 shadow-[0_16px_40px_rgb(0_0_0/0.2)] outline-none">
          <Dialog.Title className="text-lg font-semibold">Keys</Dialog.Title>
          <Dialog.Description className="mt-1 text-ink-2">
            Every action has a key. The mouse is for the page.
          </Dialog.Description>
          <div className="mt-4 grid gap-4">
            {KEYS.map((group) => (
              <section key={group.screen}>
                <h3 className="text-xs font-semibold text-ink-3">{group.screen}</h3>
                <dl className="mt-1 grid grid-cols-[120px_1fr] gap-x-4 gap-y-1">
                  {group.keys.map(([keys, label]) => (
                    <div key={label} className="contents">
                      <dt className="flex gap-0.5">
                        {keys.map((k) => (
                          <Key key={k} k={k} />
                        ))}
                      </dt>
                      <dd className="text-ink-2">{label}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            ))}
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
