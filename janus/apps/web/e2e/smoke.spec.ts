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

test("shows validation errors without structured diagnostics", async ({
  page,
}) => {
  await page.route("**/api/reviews/validate", (route) =>
    route.fulfill({
      status: 413,
      json: {
        detail: {
          code: "REFERENCE_TOO_LARGE",
          message: "The reference is too large.",
        },
      },
    }),
  );
  await page.goto("/");
  await page
    .getByLabel("Select Comparison specification (JSON)")
    .setInputFiles({
      name: "spec.json",
      mimeType: "application/json",
      buffer: Buffer.from("{}"),
    });
  await page
    .getByLabel("Select Reference dataset (JSON, CSV, or XLSX)")
    .setInputFiles({
      name: "ref.json",
      mimeType: "application/json",
      buffer: Buffer.from("{}"),
    });
  await page.getByRole("button", { name: "Validate inputs" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "The reference is too large." }),
  ).toBeVisible();
});

test("starts a review with one submission and shows result load errors", async ({
  page,
}) => {
  let validationRequests = 0;
  await page.route("**/api/reviews/validate", (route) => {
    validationRequests++;
    return route.fulfill({
      json: { valid: true, field_count: 1, specification_version: "1" },
    });
  });
  const review = {
    id: "a".repeat(32),
    status: "ready",
    document_filename: "order.pdf",
  };
  await page.route("**/api/reviews", (route) =>
    route.fulfill({ json: route.request().method() === "POST" ? review : [] }),
  );
  await page.route(`**/api/reviews/${review.id}`, (route) =>
    route.fulfill({ json: review }),
  );
  await page.route(`**/api/reviews/${review.id}/progress`, (route) =>
    route.fulfill({
      json: {
        status: "ready",
        progress_percent: 100,
        current_stage_detail: "Review ready",
      },
    }),
  );
  await page.route(`**/api/reviews/${review.id}/result`, (route) =>
    route.fulfill({
      status: 404,
      json: {
        detail: {
          code: "RESULT_NOT_FOUND",
          message: "Review result not found.",
        },
      },
    }),
  );
  await page.goto("/");
  await page.getByLabel("Select Source document (PDF)").setInputFiles({
    name: "order.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-"),
  });
  await page
    .getByLabel("Select Comparison specification (JSON)")
    .setInputFiles({
      name: "spec.json",
      mimeType: "application/json",
      buffer: Buffer.from("{}"),
    });
  await page
    .getByLabel("Select Reference dataset (JSON, CSV, or XLSX)")
    .setInputFiles({
      name: "ref.json",
      mimeType: "application/json",
      buffer: Buffer.from("{}"),
    });
  await page.getByRole("button", { name: "Start Review" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Review result not found.",
  );
  expect(validationRequests).toBe(0);
});
