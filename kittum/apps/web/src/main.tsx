import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { makeMockWorkbench } from "./data/mock/index.ts";
import { createAppRouter } from "./router.tsx";
import "./styles.css";
import { applyStoredTheme } from "./ui/theme.ts";

applyStoredTheme();

// The mock stands in for the server until WP-15 exposes the screening, documents, and extraction
// routes; swapping it is a one-line change here.
const router = createAppRouter(makeMockWorkbench({ latency: { queue: 120, page: 60, other: 40 } }));

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

createRoot(root).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
