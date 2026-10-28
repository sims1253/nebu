// Captures every workbench screen in light and dark at desktop and laptop sizes, driven by the
// keyboard into the states that matter. Run `pnpm dev` first, then `pnpm screenshots` from
// apps/web. Output: screenshots/<screen>-<theme>-<size>.png.
import { mkdirSync } from "node:fs";
import { join } from "node:path";

import { chromium } from "playwright";
import type { Page } from "playwright";

const base = process.env["KITTUM_WEB_URL"] ?? "http://localhost:5180";
const out = join(import.meta.dirname, "..", "screenshots");
const project = "/p/ivosidenib-cca";

const SIZES = { desktop: { width: 1440, height: 900 }, laptop: { width: 1280, height: 800 } };
const THEMES = ["light", "dark"] as const;

interface Shot {
  readonly name: string;
  readonly path: string;
  readonly ready: string;
  readonly keys?: ReadonlyArray<string>;
  readonly act?: (page: Page) => Promise<void>;
}

const SHOTS: ReadonlyArray<Shot> = [
  {
    name: "projects",
    path: "/",
    ready: "text=Choose a review",
    act: (page) => page.getByText(/^Server /).waitFor(),
  },
  {
    name: "title-abstract",
    path: `${project}/screening/title-abstract`,
    ready: "[data-testid=record-title]",
  },
  {
    name: "title-abstract-exclude",
    path: `${project}/screening/title-abstract`,
    ready: "[data-testid=record-title]",
    keys: ["i", "e"],
  },
  {
    name: "title-abstract-reveal",
    path: `${project}/screening/title-abstract`,
    ready: "[data-testid=record-title]",
    keys: ["i", "i", "e", "2"],
  },
  {
    name: "title-abstract-no-abstract",
    path: `${project}/screening/title-abstract`,
    ready: "[data-testid=record-title]",
    act: (page) => advanceTo(page, "HERIZON"),
  },
  {
    name: "full-text",
    path: `${project}/screening/full-text`,
    ready: "text=Page 1 of 12",
    keys: ["y", "]", "]", "]", "Enter", "]"],
  },
  {
    name: "extraction",
    path: `${project}/extraction`,
    ready: "[data-testid=field-row]",
    keys: Array(8).fill("Tab"),
  },
  {
    name: "keys",
    path: `${project}/screening/title-abstract`,
    ready: "[data-testid=record-title]",
    keys: ["h"],
  },
  {
    name: "project-switcher",
    path: `${project}/screening/title-abstract`,
    ready: "[data-testid=record-title]",
    act: (page) => page.getByRole("button", { name: /Ivosidenib/ }).click(),
  },
  {
    name: "empty-queue",
    path: "/p/durvalumab-btc-jca/screening/title-abstract",
    ready: "text=No records to screen",
  },
  ...["protocol", "search", "studies", "risk-of-bias", "progress", "reports"].map((screen) => ({
    name: `placeholder-${screen}`,
    path: `${project}/${screen}`,
    ready: "text=Not built yet",
  })),
];

/** Marks records unclear (keeping through any reveal) until the title contains `text`. */
async function advanceTo(page: Page, text: string) {
  const title = page.getByTestId("record-title");
  for (let i = 0; i < 30 && !(await title.textContent())?.includes(text); i++) {
    await page.keyboard.press("u");
    await page.waitForTimeout(80);
    if (await page.getByTestId("ai-reveal").isVisible()) await page.keyboard.press("Space");
    await page.waitForTimeout(80);
  }
}

const browser = await chromium.launch();
mkdirSync(out, { recursive: true });
for (const theme of THEMES) {
  for (const [size, viewport] of Object.entries(SIZES)) {
    const context = await browser.newContext({
      viewport,
      deviceScaleFactor: 1,
      colorScheme: theme,
    });
    await context.addInitScript((t) => localStorage.setItem("kittum.theme", t), theme);
    for (const shot of SHOTS) {
      const page = await context.newPage();
      await page.goto(base + shot.path);
      await page.locator(shot.ready).first().waitFor();
      for (const key of shot.keys ?? []) {
        await page.keyboard.press(key === " " ? "Space" : key);
        await page.waitForTimeout(120);
      }
      await shot.act?.(page);
      // Let page images decode and the reveal settle.
      await page.waitForFunction(() => [...document.images].every((image) => image.complete));
      await page.waitForTimeout(500);
      await page.screenshot({ path: join(out, `${shot.name}-${theme}-${size}.png`) });
      await page.close();
    }
    await context.close();
  }
}
await browser.close();
console.log(`wrote ${SHOTS.length * 4} screenshots to ${out}`);
