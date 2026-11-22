/**
 * E2E: chats in the sidebar (create, switch, rename, search, delete), and Stop keeping the partial answer.
 */
import { expect, test } from "@playwright/test";
import { open, resetAll, send } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("create, switch, rename, search and delete chats", async ({ page }) => {
  await open(page);
  await send(page, "Budget for the mobile app");
  await expect(page.getByTestId("assistant-message")).toContainText("Echo from gemma4:e4b");
  const firstUrl = page.url();

  await page.getByTestId("new-chat").click();
  await expect(page.getByText("What can I help with?")).toBeVisible();
  await send(page, "Hiring plan for QA");
  await expect(page.getByTestId("assistant-message")).toContainText("Hiring plan for QA");

  const items = page.getByTestId("session-item");
  await expect(items).toHaveCount(2);

  // Switch back to the first chat
  await items.filter({ hasText: "Budget for the mobile app" }).getByRole("link").click();
  await expect(page).toHaveURL(firstUrl);
  await expect(page.getByTestId("user-message")).toHaveText("Budget for the mobile app");

  // Rename
  const budget = items.filter({ hasText: "Budget for the mobile app" });
  await budget.hover();
  await budget.getByTestId("session-menu").click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  await page.getByTestId("rename-input").fill("Mobile budget review");
  await page.getByTestId("rename-input").press("Enter");
  await expect(items.filter({ hasText: "Mobile budget review" })).toHaveCount(1);

  // Search (matches titles and message text)
  await page.getByLabel("Search chats").fill("QA");
  await expect(items).toHaveCount(1);
  await expect(items.first()).toContainText("Hiring plan for QA");
  await page.getByLabel("Search chats").fill("nothing-matches-this");
  await expect(page.getByText("No chats match")).toBeVisible();
  await page.getByLabel("Clear search").click();
  await expect(items).toHaveCount(2);

  // Delete
  const hiring = items.filter({ hasText: "Hiring plan for QA" });
  await hiring.hover();
  await hiring.getByTestId("session-menu").click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(items).toHaveCount(1);
  await expect(page.getByText("Chat deleted")).toBeVisible();
});

test("stop generating keeps the partial answer visible", async ({ page }) => {
  await open(page);
  await send(page, "please be slow");
  await expect(page.getByTestId("assistant-draft")).toContainText("word3");
  await page.getByTestId("stop").click();
  await expect(page.getByText("Stopped. This reply wasn’t saved.")).toBeVisible();
  await expect(page.getByTestId("send")).toBeVisible();
});
