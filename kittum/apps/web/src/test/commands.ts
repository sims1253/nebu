// Browser commands that run in the test server against Playwright's page.
import type { BrowserCommand } from "vite-plus/test/node";

interface Playwright {
  getPage(sessionId: string): {
    keyboard: { down(key: string): Promise<void>; up(key: string): Promise<void> };
  };
}

/**
 * Holds a key down through `repeats` auto-repeats, then releases it. Playwright marks a keydown
 * of a key that is already down as `repeat: true`, as a held key does; `userEvent.keyboard`
 * releases a key before pressing it again, so it never repeats.
 */
export const holdKey: BrowserCommand<[key: string, repeats: number]> = async (
  context,
  key,
  repeats,
) => {
  const frame = await context.frame();
  await frame.evaluate(() => window.focus());
  const page = (context.provider as unknown as Playwright).getPage(context.sessionId);
  for (let i = 0; i <= repeats; i++) await page.keyboard.down(key);
  await page.keyboard.up(key);
};
