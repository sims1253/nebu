import React from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "@tanstack/react-router";
import {
  MutationCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { router } from "./router";
import { toast } from "./components/ui/toast";

import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
    },
  },
  // Surface every failed mutation — uploads, annotations, deletes — instead of
  // letting them fail silently while the UI shows stale state.
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      const fallbackTitle =
        typeof mutation.meta?.["errorTitle"] === "string"
          ? mutation.meta["errorTitle"]
          : "Request failed";
      toast.error(fallbackTitle, {
        description: error.message,
      });
    },
  }),
});

const container = document.getElementById("root");
if (!container) throw new Error("Root element not found");

createRoot(container).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
