/**
 * E2E: importing a claude.ai export: preview, choosing items, re-import without duplicates, deleting
 * imported data.
 */
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { FIXTURES, open, openSettingsTab, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

/** Uploads fixture files (and/or pasted text) in Settings → Import and waits for the preview. */
async function preview(page: Page, files: string[], text = "") {
  await openSettingsTab(page, "import");
  await expect(page.getByTestId("import-instructions")).toContainText("Export data");
  if (files.length) await page.getByTestId("import-file-input").setInputFiles(files.map((f) => `${FIXTURES}/${f}`));
  if (text) await page.getByTestId("import-text").fill(text);
  await page.getByTestId("import-preview-button").click();
  await expect(page.getByTestId("import-preview")).toBeVisible();
}

test("valid ZIP: preview, deselect, import — chats appear tagged in the sidebar", async ({ page }) => {
  await open(page);
  await preview(page, ["claude-export.zip"]);
  const summary = page.getByTestId("import-summary");
  await expect(summary).toContainText("3 conversations");
  await expect(summary).toContainText("3 people");
  await expect(page.getByTestId("import-date-range")).toHaveText("2026-08-15 → 2026-11-05");
  await expect(page.getByTestId("import-warnings")).toContainText("Skipped 1 file");
  await expect(page.getByTestId("import-people")).toContainText("release manager");
  await page.getByLabel("Import Team lunch ideas").uncheck();
  await expect(page.getByTestId("import-conversations-count")).toHaveText("2 of 3 selected");
  await page.getByTestId("import-commit").click();
  await expect(page.getByText(/Imported \d+ new items/)).toBeVisible();
  await expect(page.getByTestId("import-status")).toContainText("2 conversations");
  await page.keyboard.press("Escape");
  const items = page.getByTestId("session-item");
  await expect(items).toHaveCount(2);
  await expect(items.filter({ hasText: "Atlas go-live checklist" }).getByTestId("imported-tag")).toBeVisible();
  await expect(items.filter({ hasText: "Team lunch ideas" })).toHaveCount(0);
});

test("individual JSON file from the export", async ({ page }) => {
  await open(page);
  await preview(page, ["conversations.json"]);
  await expect(page.getByTestId("import-summary")).toContainText("2 conversations");
  await expect(page.getByTestId("import-summary")).toContainText("0 memories");
  await page.getByTestId("import-commit").click();
  await expect(page.getByTestId("import-status")).toContainText("2 conversations · 0 memories");
});

test("memory-only export: uploaded Markdown and pasted text land in Memory", async ({ page }) => {
  await open(page);
  await preview(page, ["claude-memory.md"], "Owen is a QA tester on the Payments team.");
  await expect(page.getByTestId("import-summary")).toContainText("5 memories");
  await expect(page.getByTestId("import-summary")).toContainText("0 conversations");
  await page.getByTestId("import-commit").click();
  await expect(page.getByTestId("import-status")).toContainText("5 memories");
  await page.getByTestId("settings-tab-memory").click();
  const mai = page.getByTestId("person-row").filter({ hasText: "Mai Tran" });
  await expect(mai).toContainText("backend developer");
  await expect(mai).toContainText("Payments team");
  await expect(mai).toContainText("claude.ai");
  // imported facts carry their source; edit and delete one (searching expands the matching entries)
  await page.getByLabel("Search memory").fill("tables");
  const fact = page.getByTestId("memory-fact").filter({ hasText: "Prefers short answers with tables." });
  await expect(fact).toContainText("Imported from Claude");
  await fact.getByLabel("Edit fact: Prefers short answers with tables.").click();
  await page.getByLabel("Edit fact", { exact: true }).fill("Prefers short answers.");
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByLabel("Search memory").fill("short answers");
  const edited = page.getByTestId("memory-fact").filter({ hasText: "Prefers short answers." });
  await expect(edited).toContainText("edited");
  await edited.getByLabel("Delete fact: Prefers short answers.").click();
  await expect(page.getByTestId("memory-fact").filter({ hasText: "Prefers short answers." })).toHaveCount(0);
});

test("re-importing a newer export updates entries instead of duplicating them", async ({ page }) => {
  await open(page);
  await preview(page, ["claude-export.zip"]);
  await page.getByTestId("import-commit").click();
  await expect(page.getByTestId("import-status")).toContainText("3 conversations");
  const before = await page.getByTestId("import-status").textContent();
  await page.getByTestId("import-file-input").setInputFiles(`${FIXTURES}/claude-export-newer.zip`);
  await page.getByTestId("import-preview-button").click();
  const convs = page.getByTestId("import-conversations");
  await expect(convs.locator("li").filter({ hasText: "Atlas go-live checklist" })).toContainText("Update");
  await expect(convs.locator("li").filter({ hasText: "Team lunch ideas" })).toContainText("Already imported");
  await expect(page.getByTestId("import-memory").locator("li").filter({ hasText: "Orion" })).toContainText("replaces");
  await page.getByTestId("import-commit").click();
  await expect(page.getByText(/Imported 0 new items, updated \d+/)).toBeVisible();
  await expect(page.getByTestId("import-status")).toContainText(before!.split(" · last")[0]);
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("session-item")).toHaveCount(3);
  await page.getByTestId("session-item").filter({ hasText: "Atlas go-live checklist" }).getByRole("link").click();
  await expect(page.getByTestId("assistant-message").last()).toContainText("Mai owns the rollback");
});

test("malformed files are reported, not imported", async ({ page }) => {
  await open(page);
  await preview(page, ["broken.zip", "bad.json"]);
  const warnings = page.getByTestId("import-warnings");
  await expect(warnings).toContainText("not a valid ZIP");
  await expect(warnings).toContainText("not valid JSON");
  await expect(page.getByTestId("import-commit")).toBeDisabled();
});

test("delete all imported data, with confirmation", async ({ page }) => {
  await open(page);
  await preview(page, ["claude-export.zip"]);
  await page.getByTestId("import-commit").click();
  await expect(page.getByTestId("import-status")).toContainText("3 conversations");
  await page.getByTestId("delete-imported").click();
  await expect(page.getByRole("dialog", { name: "Delete all imported data?" })).toBeVisible();
  await page.getByRole("button", { name: "Delete all" }).click();
  await expect(page.getByTestId("import-status")).toContainText("0 conversations · 0 memories · 0 people");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("imported-tag")).toHaveCount(0);
});

test("memory-only export as a folder of Markdown files (same file name in two folders)", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await open(page);
  await preview(page, ["claude-memory-folder.zip"]);
  await expect(page.getByTestId("import-file").filter({ hasText: "areas/job-search.md" })).toHaveCount(2);
  await expect(page.getByTestId("import-memory")).not.toContainText("description:");
  await expect(page.getByTestId("import-memory")).toContainText("Release management › Team");
  const people = page.getByTestId("import-people");
  await expect(people).toContainText("Priya Shah");
  await expect(people.locator("li").filter({ hasText: "Sam Rivera" })).toContainText("(you)");
  await page.getByTestId("import-commit").click();
  await expect(page.getByTestId("import-status")).toContainText("people");
  await page.getByTestId("settings-tab-memory").click();
  await expect(page.getByTestId("person-row").filter({ hasText: "Priya Shah" })).toContainText("aka Priya");
  expect(errors.filter((e) => e.includes("same key"))).toEqual([]);
});
