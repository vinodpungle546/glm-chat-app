/**
 * E2E: correcting a meeting with other transcripts of it. Pasted text with speaker names fixes a word and names the
 * speakers; the change is underlined, can be reverted and brought back; Undo all and Apply again; a Voice Memos
 * recording is "transcribed" (fake transcriber) and then corrects; unrelated text changes nothing. The fake
 * transcriber runs the real correction engine. Synthetic meeting only.
 */
import { expect, test } from "@playwright/test";
import { addProvider, ADMIN, open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

// The stand-up meeting (t-standup) as another transcript would have it: with names, and "Thursday", not "Friday".
const WITH_NAMES = `Priya: Morning all. Owen, can you give us the test update?
Owen: Sure. Regression passed on Android, two bugs left on iOS payments.
Priya: Thanks, Owen. Anything on the API?
Sam: The refund endpoint fix is merged, I'll deploy it to staging today.
Priya: Great. Owen, please retest refunds after the deploy. Due Thursday.`;

test("pasted text corrects words and names speakers; revert, bring back, undo all, apply again", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("tab-corrections").click();
  await expect(page.getByTestId("corrections-empty")).toContainText("None is needed");

  await page.getByTestId("add-transcript").click();
  await page.getByTestId("ref-source").selectOption("claude");
  await page.getByTestId("ref-text").fill(WITH_NAMES);
  await page.getByTestId("ref-add-text").click();
  await expect(page.getByText("Transcript corrected")).toBeVisible();
  await expect(page.getByText(/1 word corrected · 3 names suggested/).first()).toBeVisible();

  const last = page.locator('[data-seg="4"]');
  await expect(last.getByTestId("corrected-word")).toHaveText("Thursday.");
  await expect(last.getByTestId("corrected-word")).toHaveAttribute("title", /was “Friday\.” · from claude\.ai/);
  await expect(page.getByTestId("segments")).toContainText("Priya");  // names from the pasted transcript
  await expect(page.getByTestId("segments")).toContainText("Sam");

  const panel = page.getByTestId("corrections");
  await expect(panel.getByTestId("correction-ref")).toContainText("claude.ai");
  await panel.getByTestId("corrections-view-changes").click();
  await expect(panel.getByTestId("correction-ref-status")).toContainText("98% of words matched");
  await expect(panel.getByTestId("corrections-summary")).toHaveText("1 word corrected · 3 names suggested");
  const change = panel.getByTestId("correction-change");
  await expect(change).toContainText("Friday.");
  await change.getByTestId("correction-toggle").click();  // revert
  await expect(change).toHaveAttribute("data-reverted", "true");
  await expect(last).toContainText("Due Friday.");
  await change.getByTestId("correction-toggle").click();  // bring back
  await expect(last).toContainText("Due Thursday.");

  await panel.getByTestId("corrections-undo").click();
  await expect(last).toContainText("Due Friday.");
  await expect(last.getByTestId("corrected-word")).toHaveCount(0);
  await page.getByTestId("corrections-apply").click();
  await expect(last).toContainText("Due Thursday.");
});

test("a Voice Memos recording is transcribed, then corrects the meeting", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { ref_seconds: 1.5 } });
  await open(page, "/meetings/t-standup");
  await page.getByTestId("add-transcript").click();
  await page.getByTestId("ref-tab-audio").click();
  await page.getByTestId("ref-audio-input").setInputFiles({ name: "New Recording 7.m4a", mimeType: "audio/mp4", buffer: Buffer.alloc(2048, 1) });
  await expect(page.getByText("Transcribing the recording")).toBeVisible();
  await page.getByTestId("tab-corrections").click();
  await expect(page.getByTestId("correction-ref")).toContainText("New Recording 7");
  await expect(page.getByText("Transcript corrected")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('[data-seg="4"]')).toContainText("Due Thursday.");
  await expect(page.getByTestId("correction-ref-status")).toContainText("started 12 s before the recording");  // its clock runs 12 s ahead
});

test("unrelated text changes nothing", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("add-transcript").click();
  await page.getByTestId("ref-text").fill("The quarterly marketing budget covers paid search, events and the spring campaign launch in April, plus agency fees.");
  await page.getByTestId("ref-add-text").click();
  await expect(page.getByText(/doesn't match this meeting/).first()).toBeVisible();
  await expect(page.locator('[data-seg="4"]')).toContainText("Due Friday.");
});

test("by sentence: what each line said and says now; revert a whole sentence", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("add-transcript").click();
  await page.getByTestId("ref-text").fill(WITH_NAMES);
  await page.getByTestId("ref-add-text").click();
  await expect(page.getByText("Transcript corrected")).toBeVisible();
  await page.getByTestId("tab-corrections").click();
  const sentence = page.getByTestId("correction-sentence").filter({ hasText: "Due Thursday." });
  await expect(sentence).toContainText("Great. Owen, please retest refunds after the deploy. Due Friday.");
  await sentence.getByTestId("correction-sentence-revert").click();
  await expect(page.locator('[data-seg="4"]')).toContainText("Due Friday.");
  await page.getByTestId("corrections-view-changes").click();
  await expect(page.getByTestId("correction-change").filter({ hasText: "Friday." })).toHaveAttribute("data-reverted", "true");
});

// The other transcript doesn't have five of Quorum's words; the engine alone never removes that many on one
// reference's word (at most 3), so this is left to the model.
const LONGER = WITH_NAMES.replace("The refund endpoint fix is merged, I'll deploy it to staging today.",
  "The refund endpoint fix is merged today.");

test("a local model reviews the sentences after the engine; invented text is dropped", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("add-transcript").click();
  await page.getByTestId("ref-source").selectOption("claude");
  await page.getByTestId("ref-text").fill(LONGER);
  await page.getByTestId("correction-model-select").selectOption("local:gemma4:e4b");
  await expect(page.getByTestId("correction-model-cloud")).toHaveCount(0);
  await page.getByTestId("ref-add-text").click();
  await expect(page.getByText("reviewed the transcript")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/1 sentence fix · 1 proposal dropped/)).toBeVisible();
  const line = page.locator('[data-seg="3"]');
  await expect(line).toHaveText(/The refund endpoint fix is merged today\.$/);
  await expect(page.locator('[data-seg="1"]')).toContainText("Sure. Regression passed on Android");  // other lines untouched
  await expect(page.locator("[data-testid=segments]")).not.toContainText("escalate everything");

  await page.getByTestId("tab-corrections").click();
  await expect(page.getByTestId("correction-review-done")).toContainText("1 fix applied");
  await expect(page.getByTestId("correction-sentence").filter({ hasText: "merged today." })).toContainText("model fix");
  await page.getByTestId("correction-review-remove").click();
  await expect(line).toContainText("I'll deploy it to staging today.");
});

test("a cloud model for the review warns about confidentiality and sends nothing until you agree", async ({ page, request }) => {
  await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await open(page, "/meetings/t-standup");
  await page.getByTestId("add-transcript").click();
  await page.getByTestId("ref-text").fill(LONGER);
  await page.getByTestId("ref-add-text").click();
  await expect(page.getByText("Transcript corrected")).toBeVisible();
  await page.getByTestId("tab-corrections").click();
  const select = page.locator("#correction-model-panel");
  await select.selectOption("cloud:anthropic:claude-opus-5-5");
  await expect(page.getByTestId("correction-review").getByTestId("correction-model-cloud")).toContainText("would be sent there");
  await page.getByTestId("correction-review-run").click();
  await expect(page.getByTestId("review-cloud-warning")).toContainText("the other transcripts you added");
  await page.getByTestId("review-cloud-cancel").click();
  const sent = (await (await request.get(`${ADMIN}/cloud-requests`)).json()).filter((r: { path: string }) => r.path === "/v1/messages");
  expect(sent).toHaveLength(0);
});
