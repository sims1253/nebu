import { expect, test } from "@playwright/test";

test("shows the generic three-input workflow", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Start a document comparison" }),
  ).toBeVisible();
  await expect(page.getByLabel("Select Source document (PDF)")).toBeAttached();
  await expect(
    page.getByLabel("Select Comparison specification (JSON)"),
  ).toBeAttached();
  await expect(
    page.getByLabel("Select Reference dataset (JSON, CSV, or XLSX)"),
  ).toBeAttached();
});
