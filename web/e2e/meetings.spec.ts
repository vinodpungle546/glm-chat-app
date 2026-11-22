/**
 * E2E: transcribing recordings: upload with live progress, the watch folder, cancel, unsupported and
 * silent files, and conversation text.
 */
import { expect, test } from "@playwright/test";
import { ADMIN, open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

/** A fake 2.5 MB video file to upload. */
const video = (name: string) => ({ name, mimeType: "video/quicktime", buffer: Buffer.alloc(2_500_000, 1) });

test("upload a video, start transcription with live progress; a new start clears the previous output", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 5 } });
  await open(page, "/meetings");
  await page.getByTestId("upload-input").setInputFiles(video("Screen Recording team sync.mov"));
  await expect(page.getByText("Uploaded:")).toBeVisible();
  await expect(page.getByText("Screen Recording team sync.mov").first()).toBeVisible();
  await page.getByLabel("Speakers", { exact: true }).selectOption("3");
  await page.getByTestId("start-transcription").click();

  const panel = page.getByTestId("job-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId("job-stage")).toContainText(/Transcribing|Loading models|Waiting/);
  await expect(panel.getByTestId("live-transcript")).toContainText("Live line 1");
  await expect(panel).toHaveAttribute("data-status", "done", { timeout: 15_000 });
  await expect(panel.getByTestId("job-result")).toContainText("Owen, can you give us the test update?");

  // Start again from the watch folder: the previous output disappears before the new run shows.
  await page.getByTestId("source-folder").click();
  await page.getByRole("option", { name: /standup\.mp4/ }).click();
  await page.getByTestId("start-transcription").click();
  await expect(page.getByTestId("job-result")).toHaveCount(0);
  await expect(page.getByTestId("job-panel")).toHaveAttribute("data-status", /queued|running/);
});

test("cancel a running transcription", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 30 } });
  await open(page, "/meetings");
  await page.getByTestId("source-folder").click();
  await page.getByRole("option", { name: /standup\.mp4/ }).click();
  await page.getByTestId("start-transcription").click();
  await expect(page.getByTestId("job-panel")).toHaveAttribute("data-status", "running");
  await page.getByTestId("cancel-job").click();
  await expect(page.getByTestId("job-panel")).toHaveAttribute("data-status", "cancelled");
  await expect(page.getByText(/Cancelled\. Starting the same recording again resumes/)).toBeVisible();
});

test("unsupported file type and a silent screen recording are explained", async ({ page }) => {
  await open(page, "/meetings");
  await page.getByTestId("upload-input").setInputFiles({ name: "notes.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") });
  await expect(page.getByTestId("upload-error")).toContainText("Unsupported file type “.exe”");

  await page.getByTestId("source-folder").click();
  await page.getByRole("option", { name: /Screen Recording silent\.mov/ }).click();
  await expect(page.getByTestId("no-audio")).toContainText("no sound");
  await expect(page.getByTestId("start-transcription")).toHaveCount(0);
});

test("conversation text becomes a transcript", async ({ page }) => {
  await open(page, "/meetings");
  await page.getByTestId("source-text").click();
  await page.getByLabel("Title").fill("Retro notes");
  await page.getByTestId("conversation-text").fill("Priya: Let's start the retro.\nOwen: Testing went well.\nPriya: Great, thanks Owen.");
  await page.getByTestId("import-text").click();
  await expect(page).toHaveURL(/\/meetings\/[0-9a-f]+/);
  await expect(page.getByTestId("transcript-title")).toHaveText("Retro notes");
  await expect(page.getByTestId("segments")).toContainText("Testing went well.");
});
