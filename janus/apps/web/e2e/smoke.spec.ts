import { expect, test } from "@playwright/test";

for (const outcome of ["ready", "error"] as const) {
  test(`tracks review stages through ${outcome}`, async ({ page }) => {
    const review = {
      id: "b".repeat(32),
      status: "reading_document",
      document_filename: "order.pdf",
      specification_version: "1",
      error_message: "Could not read the PDF.",
    };
    let resultRequests = 0;
    await page.route(`**/api/reviews/${review.id}`, (route) =>
      route.fulfill({ json: review }),
    );
    await page.route(`**/api/reviews/${review.id}/result`, (route) => {
      resultRequests++;
      return route.fulfill({
        json: { comparisons: [], summary: { total_fields: 0 } },
      });
    });
    await page.route(`**/api/reviews/${review.id}/document`, (route) =>
      route.fulfill({ contentType: "application/pdf", body: "%PDF-" }),
    );
    await page.goto(`/reviews/${review.id}`);
    const stage = page.getByRole("status", { name: "Review stage" });
    await expect(stage).toHaveText("Reading PDF…");
    review.status = "extracting_fields";
    await expect(stage).toHaveText("Extracting fields…");
    expect(resultRequests).toBe(0);

    review.status = outcome;
    if (outcome === "ready") {
      await expect(
        page.getByText("No fields match this filter."),
      ).toBeVisible();
      expect(resultRequests).toBeGreaterThan(0);
    } else {
      await expect(page.getByRole("alert")).toHaveText(review.error_message);
      expect(resultRequests).toBe(0);
    }
  });
}

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
