/**
 * E2E: summaries with local and cloud models (cloud asks first), long meetings in parts, speaker labels
 * and corrections.
 */
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { chooseSummaryModel, open, pickModel, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

/** "Generate summary", or "Regenerate" when an earlier test already saved one. */
async function startSummary(page: Page, model = "local:gemma4:e4b") {
  await page.getByTestId("tab-summary").click();
  const panel = page.getByTestId("summary-panel");
  await expect(panel.getByRole("button", { name: /Generate notes|Generate summary|Regenerate/ }).first()).toBeVisible();
  const regenerate = panel.getByRole("button", { name: "Regenerate" });
  if (await regenerate.isVisible()) await regenerate.click();
  else await page.getByTestId("generate-summary").click();
  await chooseSummaryModel(page, model);  // no summary model chosen yet: the app asks
}

test("summary with the local model (Gemma 4 E4B)", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await startSummary(page);
  const panel = page.getByTestId("summary-panel");
  await expect(panel).toContainText("written by gemma4:e4b", { timeout: 15_000 });
  await expect(panel).toContainText("Gemma 4 E4B · local");
  await expect(panel.getByRole("table")).toContainText("Retest refunds");
});

test("summary with the cloud model asks first, then uses it", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await pickModel(page, "cloud:gaia");
  await startSummary(page, "cloud:gaia");
  await expect(page.getByText("Send this meeting to the cloud?")).toBeVisible();
  await page.getByTestId("summary-cloud").click();
  await expect(page.getByTestId("summary-panel")).toContainText("written by GLM-5.3 Flash (cloud)");
  await expect(page.getByTestId("summary-panel")).toContainText("GLM-5.3 Flash · cloud");
});

test("a long transcript is summarised in parts, then merged", async ({ page }) => {
  await open(page, "/meetings/t-long");
  await startSummary(page);
  await expect(page.getByTestId("summary-notice")).toContainText(/extracting notes from \d+ parts/);
  await expect(page.getByTestId("summary-progress")).toBeVisible();
  await expect(page.getByTestId("summary-panel")).toContainText(/combined from \d+ parts/, { timeout: 40_000 });
  await expect(page.getByTestId("notes")).toContainText("Review the roadmap, budget and hiring plan"); // merged once
  await expect(page.getByTestId("notes").getByRole("cell", { name: "Review the roadmap, budget and hiring plan" })).toHaveCount(1);
});

test("speaker labels with confidence, manual correction saved to memory", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("identify-speakers").click();
  const rows = page.getByTestId("speaker-row");
  await expect(rows.filter({ hasText: "Owen" })).toHaveCount(1);
  const owen = page.locator('[data-speaker="SPEAKER_01"]');
  await expect(owen.getByTestId("speaker-name")).toHaveText("Owen");
  await expect(owen.getByTestId("confidence")).toHaveText(/(High|Medium) · \d+%/);
  await expect(owen).toContainText("QA tester");
  await owen.getByRole("button", { name: "Why" }).click();
  await expect(owen).toContainText("answers when Owen is addressed");

  // Correct SPEAKER_02 (no name guessed) → confirmed, and remembered in Memory → People
  const dev = page.locator('[data-speaker="SPEAKER_02"]');
  await dev.getByTestId("edit-speaker").click();
  await dev.getByTestId("speaker-name-input").fill("Mai");
  await dev.getByLabel("Role for SPEAKER_02").fill("Backend developer");
  await dev.getByTestId("save-speaker").click();
  await expect(dev.getByTestId("speaker-name")).toHaveText("Mai");
  await expect(dev).toContainText("Confirmed");
  await expect(page.getByTestId("segments")).toContainText("Mai");

  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-tab-memory").click();
  const mai = page.getByTestId("person-row").filter({ hasText: "Mai" });
  await expect(mai).toContainText("Backend developer");
  await expect(mai).toContainText("Voice known");
});

test("ask about a meeting opens a chat with the transcript attached", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("chat-about").click();
  await expect(page).toHaveURL(/\/c\/[0-9a-f]+/);
  await expect(page.getByTestId("composer-chips")).toContainText("Transcript: Weekly standup");
});
