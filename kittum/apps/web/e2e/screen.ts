// Screening steps the journeys share.
import { expect } from "@playwright/test";
import type { Page } from "@playwright/test";

/** Screens a person's whole title and abstract queue: includes everything, keeps it on a reveal. */
export const includeAll = async (page: Page) => {
  const title = page.getByTestId("record-title");
  const finished = page.getByRole("heading", { name: "Queue finished" });
  const banner = page.getByTestId("ai-reveal");
  // The record on screen now, or null between records and while the AI is still reading: never
  // wait for a title that may not come back (as in review.spec.ts).
  const titleNow = async () =>
    (await title.count()) > 0
      ? await title.textContent({ timeout: 1_000 }).catch(() => null)
      : null;
  for (;;) {
    await expect(title.or(finished)).toBeVisible({ timeout: 60_000 });
    if (await finished.isVisible()) return;
    const text = await titleNow();
    await page.keyboard.press("i");
    await expect
      .poll(
        async () =>
          (await banner.isVisible()) || (await finished.isVisible()) || (await titleNow()) !== text,
        { timeout: 60_000 },
      )
      .toBe(true);
    if (await banner.isVisible()) await page.keyboard.press("Space"); // keep my decision
  }
};
