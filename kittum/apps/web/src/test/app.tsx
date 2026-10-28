// Mounts the whole workbench on the mock, at a route, for flow tests.
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { createRoot } from "react-dom/client";
import { commands, userEvent } from "vite-plus/test/browser";

import { makeMockWorkbench } from "../data/mock/index.ts";
import type { MockOptions } from "../data/mock/index.ts";
import { PROJECTS, REVIEWER } from "../data/mock/protocol.ts";
import { rememberReviewer } from "../screens/project.tsx";
import { createAppRouter } from "../router.tsx";
import "../styles.css";

export function mount(path: string, options: MockOptions = {}) {
  const workbench = makeMockWorkbench(options);
  // The mock's reviewer is at the workbench in every project.
  for (const project of PROJECTS) rememberReviewer(project.id, REVIEWER);
  const router = createAppRouter(workbench, createMemoryHistory({ initialEntries: [path] }));
  const container = document.createElement("div");
  container.style.height = "900px";
  document.body.replaceChildren(container);
  const root = createRoot(container);
  root.render(<RouterProvider router={router} />);
  return { workbench, unmount: () => root.unmount() };
}

/** Presses one key with a real key event. */
export const press = (key: string) => {
  const named: Record<string, string> = {
    " ": " ",
    Backspace: "{Backspace}",
    Enter: "{Enter}",
    Escape: "{Escape}",
    Tab: "{Tab}",
    ArrowDown: "{ArrowDown}",
    ArrowUp: "{ArrowUp}",
    "[": "[[",
  };
  return userEvent.keyboard(named[key] ?? key);
};

/** Holds a key long enough for the browser to auto-repeat it `repeats` times. */
export const hold = (key: string, repeats = 4) =>
  (commands as unknown as { holdKey: (key: string, repeats: number) => Promise<void> }).holdKey(
    key,
    repeats,
  );
