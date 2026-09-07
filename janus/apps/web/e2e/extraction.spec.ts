import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

// Uses the real backend and bundled PDF, including rendered evidence and exports.
test("extract, inspect, compare, revise, and reopen a PDF", async ({
  page,
}) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Turn a PDF into structured data." }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Try purchase order" }).click();
  await expect(page).toHaveURL(/\/extractions\/[0-9a-f]{32}$/);
  const firstUrl = page.url();
  const id = firstUrl.split("/").at(-1)!;
  await expect(
    page.getByRole("heading", { name: "purchase-order.pdf" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /\/order\/total/ }),
  ).toContainText("1275.50");
  await page.getByRole("button", { name: /\/order\/total/ }).click();
  await expect(page.getByLabel("Source of /order/total")).toBeVisible();
  await expect(page.getByRole("img", { name: "PDF page 1" })).toBeVisible();
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export data", exact: true }).click();
  const download = await downloadEvent;
  expect(JSON.parse(await readFile((await download.path())!, "utf8"))).toEqual({
    order: { number: "PO-1042", total: "1275.50" },
  });
  await page.getByRole("button", { name: "Compare data", exact: true }).click();
  await page.getByLabel("Reference data", { exact: true }).setInputFiles({
    name: "reference.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify({ order: { number: "PO-1042", total: 999 } }),
    ),
  });
  await page
    .getByRole("button", { name: "Compare data", exact: true })
    .last()
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "1 matched" }),
  ).toContainText("1 mismatched");
  await expect(
    page.getByRole("button", { name: /\/order\/total/ }),
  ).toContainText("Mismatch");
  await page.getByRole("button", { name: "Close comparison" }).click();
  await page
    .getByRole("button", { name: "Specification", exact: true })
    .click();
  const editor = page.getByLabel("Specification JSON");
  await expect(editor).toContainText("Purchase order");
  const spec = JSON.parse(await editor.inputValue());
  // An unsupported number format remains an explicit issue in the new result.
  delete spec.fields.order.fields.total.group_separator;
  await editor.fill(JSON.stringify(spec));
  await page
    .getByRole("button", { name: "Extract again", exact: true })
    .click();
  await expect(page).not.toHaveURL(firstUrl);
  const secondId = page.url().split("/").at(-1)!;
  await expect(
    page.getByRole("button", { name: /\/order\/total/ }),
  ).toContainText("Invalid format");
  await page.getByRole("button", { name: "1 to inspect", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /\/order\/number/ }),
  ).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole("button", { name: /\/order\/total/ }),
  ).toContainText("Invalid format");
  await page.goto(firstUrl);
  await expect(
    page.getByRole("button", { name: /\/order\/total/ }),
  ).toContainText("1275.50");
  await page.request.delete(`/api/extractions/${id}`);
  await page.request.delete(`/api/extractions/${secondId}`);
});

test("upload failures explain the problem", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("PDF document", { exact: true }).setInputFiles({
    name: "broken.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("broken"),
  });
  await page
    .getByLabel("Extraction specification", { exact: true })
    .setInputFiles({
      name: "extraction.json",
      mimeType: "application/json",
      buffer: Buffer.from('{"name":"Invalid","fields":{}}'),
    });
  await page.getByRole("button", { name: "Extract data", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});
