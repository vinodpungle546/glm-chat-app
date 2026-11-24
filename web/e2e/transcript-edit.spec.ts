/**
 * E2E: editing the transcript yourself. Change a line's words, give a line to someone new (and the question about
 * their voice fingerprint), move several lines at once, split a line between two people, merge lines, and Undo one
 * edit or all of them. Your edits win over corrections made later. Synthetic meeting t-video.
 */
import { expect, test } from "@playwright/test";
import { open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

const line = (page: import("@playwright/test").Page, i: number) => page.locator(`[data-seg="${i}"]`);

test("edit words and speakers, split, merge and undo", async ({ page }) => {
  await open(page, "/meetings/t-video");
  await page.getByTestId("edit-mode").click();

  // words
  await line(page, 1).getByTestId("seg-text").click();
  await line(page, 1).getByTestId("seg-text-input").fill("Hi, Daniel here. The Atlas payments regression is now green.");
  await line(page, 1).getByTestId("seg-text-save").click();
  await expect(line(page, 1).getByTestId("seg-text")).toHaveText("Hi, Daniel here. The Atlas payments regression is now green.");

  // a line said by someone new: a new speaker, and the voice question
  await line(page, 4).getByTestId("seg-speaker").selectOption("__new");
  await line(page, 4).getByTestId("seg-speaker-name").fill("Sam Lee");
  await line(page, 4).getByTestId("seg-speaker-name-save").click();
  await expect(page.getByText("Update Sam Lee's voice fingerprint?")).toBeVisible();
  await page.getByTestId("voice-update").click();
  await expect(page.getByText("Updated Sam Lee's voice fingerprint")).toBeVisible();
  await expect(page.locator('[data-testid="speaker-row"]').filter({ hasText: "Sam Lee" })).toBeVisible();

  // several lines at once
  await line(page, 0).getByTestId("seg-select").check();
  await line(page, 2).getByTestId("seg-select").check();
  await expect(page.getByTestId("edit-selected")).toContainText("2 lines selected");
  const speakers = await page.getByTestId("assign-selected").locator("option").allTextContents();
  await page.getByTestId("assign-selected").selectOption({ label: speakers.find((s) => s.startsWith("Speaker 3")) ?? speakers[2] });
  await expect(page.getByText("2 lines moved")).toBeVisible();
  await expect(line(page, 0).getByTestId("seg-speaker")).toHaveValue("spk:SPEAKER_02");

  // split line 3 between two people, then merge it back
  await line(page, 3).getByTestId("seg-split").click();
  await line(page, 3).getByTestId("split-word").filter({ hasText: "refund" }).click();
  await line(page, 3).getByTestId("split-speaker").selectOption("spk:SPEAKER_01");
  await line(page, 3).getByTestId("split-save").click();
  await expect(line(page, 3).getByTestId("seg-text")).toHaveText("I'll deploy the");
  await expect(line(page, 4).getByTestId("seg-text")).toHaveText("refund fix to staging today.");
  await expect(line(page, 4).getByTestId("seg-speaker")).toHaveValue("spk:SPEAKER_01");
  await line(page, 3).getByTestId("seg-merge").click();
  await expect(line(page, 3).getByTestId("seg-text")).toHaveText("I'll deploy the refund fix to staging today.");

  // the list of your edits: undo one, then all
  await page.getByTestId("edit-mode").click();  // done editing: edited lines are marked
  await expect(line(page, 1).getByTestId("seg-edited")).toBeVisible();
  await page.getByTestId("edits-list").click();
  await expect(page.getByTestId("edit-item")).toHaveCount(5);
  await expect(page.getByTestId("edit-item").last()).toContainText("Wording:");
  await page.getByTestId("edit-item").last().getByTestId("edit-undo").click();
  await expect(line(page, 1)).toContainText("The Atlas payments regression is green.");
  await expect(page.getByTestId("edit-item")).toHaveCount(4);  // the list stays open
  await page.getByTestId("edits-undo-all").click();
  await expect(page.getByTestId("edits-list")).toHaveCount(0);
  await expect(page.locator('[data-testid="speaker-row"]').filter({ hasText: "Sam Lee" })).toHaveCount(0);
});

test("your wording stays when another transcript corrects the meeting later", async ({ page, request }) => {
  await open(page, "/meetings/t-video");
  await page.getByTestId("edit-mode").click();
  await line(page, 2).getByTestId("seg-text").click();
  await line(page, 2).getByTestId("seg-text-input").fill("Thanks. Daniel, please rerun the refund tests by Monday.");
  await line(page, 2).getByTestId("seg-text-save").click();
  await expect(line(page, 2).getByTestId("seg-text")).toContainText("by Monday");
  const res = await request.post("http://127.0.0.1:18000/api/meetings/transcripts/t-video/references",
    { data: { source: "claude", text: "Morning all, let's start the Atlas release sync.\nHi, Daniel here. The Atlas payments regression is green.\nThanks. Daniel, please rerun the refund tests by Friday." } });
  expect(res.ok()).toBeTruthy();
  await page.reload();
  await expect(line(page, 2)).toContainText("by Monday");
});
