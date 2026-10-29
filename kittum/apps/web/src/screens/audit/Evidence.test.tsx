// The Audit screen's model call view on a streamed answer: the response Kittum stored is the
// server's event stream as received, and the view adds it up to the reasoning and the answer.
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import * as Effect from "effect/Effect";
import { createRoot } from "react-dom/client";
import { afterEach, expect, test } from "vite-plus/test";
import { page } from "vite-plus/test/browser";

import { makeMockWorkbench } from "../../data/mock/index.ts";
import type { ArtifactCheck, ModelCall } from "../../data/schema.ts";
import { ModelCalls } from "./Evidence.tsx";
import "../../styles.css";

const REQUEST = "a".repeat(64);
const RESPONSE = "b".repeat(64);

// GLM-5.3-Flash through Z.ai: thinking in `reasoning_content` deltas, then the answer, a usage
// chunk without choices, and the end of the stream.
const chunk = (delta: Record<string, string>, finish: string | null = null) =>
  `data: ${JSON.stringify({ id: "c1", model: "glm-5.3-flash", choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
const STREAM = [
  chunk({ role: "assistant", reasoning_content: "The trial enrolled adolescents, " }),
  chunk({ reasoning_content: "so the adult criterion is not met." }),
  chunk({ content: '{"c0": {"status": "not_met", ' }),
  chunk({ content: '"quote": "patients aged 12 to 17 years"}}' }, "stop"),
  `data: ${JSON.stringify({ id: "c1", model: "glm-5.3-flash", choices: [], usage: { prompt_tokens: 812, completion_tokens: 795 } })}\n\n`,
  "data: [DONE]\n\n",
].join("");
const BODY = JSON.stringify({ model: "glm-5.3-flash", messages: [], max_tokens: 28096 });

let unmount: (() => void) | undefined;
afterEach(() => unmount?.());

test("a streamed response shows the model's reasoning and its answer", async () => {
  const bytes: Record<string, string> = { [REQUEST]: BODY, [RESPONSE]: STREAM };
  const workbench = {
    ...makeMockWorkbench(),
    auditArtifact: ({ sha256 }: { sha256: string }) =>
      Effect.succeed(new TextEncoder().encode(bytes[sha256])),
    checkArtifact: ({ sha256 }: { sha256: string }) =>
      Effect.succeed<ArtifactCheck>({
        sha256,
        recomputed: sha256,
        matches: true,
        stored: true,
        bytes: bytes[sha256]?.length ?? null,
        media_type: null,
      }),
  };
  const call: ModelCall = {
    number: 1,
    request: { sha256: REQUEST, form: "http_body", stored: true },
    response: { sha256: RESPONSE, form: "http_response", stored: true },
    model_identity: "glm-5.3-flash",
    finish_reason: "stop",
    problem: null,
  };
  const root = createRootRoute();
  const project = createRoute({
    getParentRoute: () => root,
    path: "/p/$projectId",
    component: () => <ModelCalls calls={[call]} caller={{ projectId: "p1", as: "lead" }} />,
  });
  const router = createRouter({
    routeTree: root.addChildren([project]),
    context: { workbench },
    history: createMemoryHistory({ initialEntries: ["/p/p1"] }),
  });
  const container = document.createElement("div");
  document.body.replaceChildren(container);
  const react = createRoot(container);
  react.render(<RouterProvider router={router as never} />);
  unmount = () => react.unmount();

  await page.getByRole("button", { name: "Show the request and the response" }).click();
  const response = page.getByTestId("call-response");
  await expect
    .element(response.getByLabelText("Reasoning"))
    .toMatchTextContent("The trial enrolled adolescents, so the adult criterion is not met.");
  await expect
    .element(response.getByLabelText("Answer", { exact: true }))
    .toMatchTextContent(/"status": "not_met",\s+"quote": "patients aged 12 to 17 years"/);
  await expect.element(response).toMatchTextContent(/prompt tokens 812, completion tokens 795/);
  // The bytes as stored are the stream itself.
  await expect
    .element(response.getByLabelText("Response bytes"))
    .toMatchTextContent(/reasoning_content.*data: \[DONE\]/);
});
