import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { makeApiWorkbench } from "./data/api.ts";
import { createAppRouter } from "./router.tsx";
import "./styles.css";
import { applyStoredTheme } from "./ui/theme.ts";

applyStoredTheme();

// The server in development and production. `VITE_KITTUM_WORKBENCH=mock pnpm dev` shows the
// recorded example data instead, without a server; the unit tests mount the mock directly.
const workbench =
  import.meta.env["VITE_KITTUM_WORKBENCH"] === "mock"
    ? (await import("./data/mock/index.ts")).makeMockWorkbench({
        latency: { queue: 120, page: 60, other: 40 },
      })
    : makeApiWorkbench();
const router = createAppRouter(workbench);

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

createRoot(root).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
