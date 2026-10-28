import * as Effect from "effect/Effect";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, describe, expect, test } from "vite-plus/test";

import type { Box, LayerSummary, PageLayer } from "../../data/schema.ts";
import type { Workbench } from "../../data/workbench.ts";
import "../../styles.css";
import { PdfViewer } from "./PdfViewer.tsx";

// A one-pixel white PNG stands in for a rendered page.
const IMAGE =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=";

let root: Root | undefined;
afterEach(() => root?.unmount());

const viewer = (layer: LayerSummary, page: Workbench["page"]) => {
  const container = document.createElement("div");
  container.style.height = "900px";
  document.body.replaceChildren(container);
  root = createRoot(container);
  const workbench = { page } as unknown as Workbench;
  return (target: { key: string; box: Box } | null) =>
    root?.render(
      <PdfViewer
        workbench={workbench}
        layer={layer}
        highlights={[]}
        target={target}
        label="Test"
      />,
    );
};

describe("PDF viewer", () => {
  test("lays out the words of a page from a long layer in one pass over its text", async () => {
    // An astral character first, so every offset after it differs between code points and
    // UTF-16, then 700 words and two million characters of the rest of a long report.
    const words = Array.from({ length: 700 }, (_, i) => `word${i}`);
    const head = `𝛼 ${words.join(" ")} `;
    const text = head + "x".repeat(2_000_000);
    const pageWords = words.map((word, i) => {
      const start = Array.from(head.slice(0, head.indexOf(` ${word} `) + 1)).length; // code points
      const x0 = (i % 20) * 28;
      const y0 = Math.floor(i / 20) * 20;
      return {
        start,
        end: start + word.length,
        box: { page: 1, x0, y0, x1: x0 + 26, y1: y0 + 12 },
      };
    });
    const layer: LayerSummary = {
      id: "layer",
      document_id: "document",
      text,
      pages: [{ page: 1, width: 600, height: 800 }],
    };
    const render = viewer(layer, () =>
      Effect.succeed<PageLayer>({
        page: 1,
        width: 600,
        height: 800,
        image_url: IMAGE,
        words: pageWords,
      }),
    );
    const started = performance.now();
    render(null);
    await expect
      .poll(() => document.querySelectorAll(".text-layer > span").length, { timeout: 2000 })
      .toBe(700);
    expect(performance.now() - started).toBeLessThan(1500);
    const word5 = document.querySelector(`.text-layer > span[data-s="${pageWords[5]!.start}"]`);
    expect(word5?.textContent).toBe("word5");
    expect(document.querySelectorAll(".text-layer > span")[699]?.textContent).toBe("word699");
  });

  test("keeps a bounded number of pages and reloads one that fell out", async () => {
    const pages = Array.from({ length: 40 }, (_, i) => ({ page: i + 1, width: 600, height: 800 }));
    const layer: LayerSummary = {
      id: "layer-40",
      document_id: "document",
      text: "",
      pages,
    };
    const loads = new Map<number, number>();
    const render = viewer(layer, (_, page) => {
      loads.set(page, (loads.get(page) ?? 0) + 1);
      return Effect.succeed<PageLayer>({
        page,
        width: 600,
        height: 800,
        image_url: IMAGE,
        words: [],
      });
    });
    const at = (page: number) => ({
      key: `p${page}`,
      box: { page, x0: 10, y0: 10, x1: 20, y1: 20 },
    });

    for (let page = 1; page <= 40; page++) {
      render(at(page));
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    expect(loads.get(1)).toBe(1);
    expect(loads.get(40)).toBe(1);

    // Page 40 was used last and stays; page 1 fell out and loads again.
    render(at(1));
    await expect.poll(() => loads.get(1)).toBe(2);
    render(at(39));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(loads.get(40)).toBe(1);
  });
});
