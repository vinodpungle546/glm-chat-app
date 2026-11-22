/**
 * E2E: imported claude.ai chats are tagged, can be hidden, are read-only, and can be continued in a new
 * chat.
 */
import { expect, test } from "@playwright/test";
import { importExport, open, pickModel, resetAll, send } from "./helpers";

test.beforeEach(async ({ request }) => {
  await resetAll(request);
  await importExport(request);
});

test("imported chats are tagged and can be hidden", async ({ page }) => {
  await open(page);
  const items = page.getByTestId("session-item");
  await expect(items).toHaveCount(3);
  await expect(page.getByTestId("imported-tag")).toHaveCount(3);
  await page.getByTestId("toggle-imported").click();
  await expect(items).toHaveCount(0);
  await expect(page.getByTestId("imported-hidden")).toBeVisible();
  await page.getByTestId("toggle-imported").click();
  await expect(items).toHaveCount(3);
});

test("an imported chat is read-only and keeps its history, title and dates", async ({ page }) => {
  await open(page);
  await page.getByTestId("session-item").filter({ hasText: "Atlas go-live checklist" }).getByRole("link").click();
  await expect(page.getByTestId("imported-banner")).toContainText("Imported from claude.ai");
  await expect(page.getByTestId("imported-banner")).toContainText("started 2026-11-05");
  await expect(page.getByTestId("user-message")).toHaveCount(2);
  await expect(page.getByTestId("assistant-message").first()).toContainText("rollback plan");
  await expect(page.getByTestId("read-only-bar")).toBeVisible();
  await expect(page.getByTestId("composer-input")).toHaveCount(0);
});

test("continue an imported chat in a new chat with the selected model", async ({ page }) => {
  await open(page);
  await pickModel(page, "cloud:gaia");
  await page.getByTestId("session-item").filter({ hasText: "Atlas go-live checklist" }).getByRole("link").click();
  await expect(page.getByTestId("continue-chat")).toHaveText(/Continue with GLM-5.3 Flash/);
  const importedUrl = page.url();
  await page.getByTestId("continue-chat").click();
  await expect(page).not.toHaveURL(importedUrl);
  await expect(page.getByTestId("continued-banner")).toContainText("Atlas go-live checklist");
  await expect(page.getByTestId("user-message")).toHaveCount(2); // history carried over
  await send(page, "Add a smoke test to the list");
  await expect(page.getByTestId("assistant-message").last()).toContainText("Cloud answer: Add a smoke test");
  // first cloud chat with imported data: one-time notice
  await expect(page.getByText(/This notice is shown once/)).toBeVisible();
  await expect(page.getByTestId("session-item").filter({ hasText: "(continued)" })).toHaveCount(1);
});
