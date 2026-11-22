/**
 * E2E: model choices for meetings: Settings → Meetings, New meeting, re-runs in the panels, the summary-
 * model question, after-transcription work, and new chats starting on the cloud model.
 */
import { expect, test } from "@playwright/test";
import { ADMIN, API, open, openSettingsTab, pickModel, resetAll } from "./helpers";

// The fake Ollama has Gemma 4 E4B (reads images) and Qwen3 8B (text only); Qwen3-VL 4B can be downloaded.
test.beforeEach(async ({ request }) => resetAll(request));

/** The current preferences, read through the API. */
const prefs = async (request: import("@playwright/test").APIRequestContext) =>
  (await (await request.get(`${API}/api/preferences`)).json()) as Record<string, unknown>;

test("Settings → Meetings: transcript model, video-frames model (vision only) and summary model", async ({ page, request }) => {
  await open(page, "/");
  await openSettingsTab(page, "speakers");
  await expect(page.getByTestId("settings-tab-speakers")).toHaveText(/Meetings/);
  const models = page.getByTestId("meeting-models");
  await expect(models).toContainText("Whisper large-v3-turbo");

  const speaker = models.getByLabel("Speaker identification (reads the transcript, on this Mac)");
  await expect(speaker).toContainText("Recommended (gemma4:e4b)");
  await expect(speaker.locator('option[value="gemma4:e4b"]')).toHaveText(/reads video/);
  await expect(speaker.locator('option[value="qwen3:8b"]')).not.toHaveText(/reads video/);
  await expect(speaker.locator('option[value^="cloud:"]')).toHaveCount(0);  // local only
  await speaker.selectOption("qwen3:8b");
  await expect(models.getByTestId("frames-note")).toContainText("Qwen3 8B reads text only, so video frames go to gemma4:e4b");
  await expect.poll(async () => (await prefs(request)).speaker_model).toBe("qwen3:8b");

  const frames = models.getByLabel("Video frames (names on the participants' tiles)");
  await expect(frames.locator('option[value="qwen3:8b"]')).toHaveCount(0);  // a text-only model can't read frames
  await frames.selectOption("qwen3-vl:4b-instruct");
  const download = models.getByTestId("model-frames-download");
  await expect(download).toContainText("Qwen3-VL 4B isn't downloaded");
  await expect(download).toContainText("Until it is, gemma4:e4b is used instead");
  await download.getByRole("button", { name: "Download Qwen3-VL 4B" }).click();
  await expect(download).toHaveCount(0, { timeout: 15_000 });
  await expect.poll(async () => (await prefs(request)).vision_model).toBe("qwen3-vl:4b-instruct");

  const summary = models.getByLabel("Meeting summaries");
  await expect(summary).toContainText("Ask me each time");
  await summary.selectOption("local:qwen3:8b");
  await expect.poll(async () => (await prefs(request)).summary_model).toBe("local:qwen3:8b");
});

test("New meeting: fixed steps shown, models chosen for this meeting, speakers and summary after transcription", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 3 } });
  await open(page, "/meetings");
  await page.getByTestId("source-folder").click();
  await page.getByRole("option", { name: /standup\.mp4/ }).click();
  const start = page.getByTestId("start-models");
  await expect(start.getByTestId("fixed-models")).toHaveText(/Whisper large-v3-turbo.*pyannote 3\.1.*fixed, on this Mac/);
  await expect(start.getByTestId("estimate-speakers")).toContainText("with gemma4:e4b");
  await start.getByLabel("Speaker identification", { exact: true }).selectOption("qwen3:8b");
  await expect(start.getByTestId("estimate-speakers")).toContainText("with qwen3:8b");
  await expect(start.getByTestId("frames-note")).toContainText("reads text only");
  await start.getByLabel("Summary", { exact: true }).selectOption("cloud:gaia");
  await expect(start.getByTestId("estimate-summary")).toContainText("cloud model, asks before the transcript is sent");
  await start.getByLabel("Summary", { exact: true }).selectOption("local:gemma4:e4b");
  await expect(start.getByTestId("estimate-summary")).toContainText(/with gemma4:e4b: about \d/);
  await start.getByTestId("start-after").check();
  await page.getByTestId("start-transcription").click();

  const followup = page.getByTestId("followup");
  await expect(followup).toHaveAttribute("data-state", "done", { timeout: 40_000 });
  await expect(followup).toContainText("Speakers by qwen3:8b, summary by Gemma 4 E4B");
  expect((await prefs(request)).speaker_model).toBe("");  // Settings didn't change

  await page.getByRole("button", { name: "Open transcript" }).click();
  await page.getByTestId("tab-speakers").click();
  await expect(page.getByTestId("speaker-model-used")).toContainText("Last identified with qwen3:8b");
  await expect(page.getByLabel("Reads the transcript")).toHaveValue("qwen3:8b");

  // Re-run with another model; the meeting keeps it.
  await page.getByLabel("Reads the transcript").selectOption("gemma4:e4b");
  await page.getByTestId("identify-speakers").click();
  await expect(page.getByTestId("speaker-model-used")).toContainText("Last identified with gemma4:e4b", { timeout: 15_000 });
  await page.reload();
  await page.getByTestId("tab-speakers").click();
  await expect(page.getByLabel("Reads the transcript")).toHaveValue("gemma4:e4b");

  await page.getByTestId("tab-summary").click();
  await expect(page.getByTestId("summary-model-used")).toContainText("Last written by Gemma 4 E4B");
  await expect(page.getByLabel("Writes the summary")).toHaveValue("local:gemma4:e4b");
});

test("a cloud summary model waits for an OK after transcription", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 3 } });
  await open(page, "/meetings");
  await page.getByTestId("source-folder").click();
  await page.getByRole("option", { name: /standup\.mp4/ }).click();
  const start = page.getByTestId("start-models");
  await start.getByLabel("Summary", { exact: true }).selectOption("cloud:gaia");
  await start.getByTestId("start-after").check();
  await page.getByTestId("start-transcription").click();
  const followup = page.getByTestId("followup");
  await expect(followup).toHaveAttribute("data-state", "needs_ok", { timeout: 40_000 });
  await page.getByRole("button", { name: "Open transcript" }).click();
  await page.getByTestId("tab-summary").click();
  await expect(page.getByTestId("summary-needs-ok")).toBeVisible();
});

test("a summary asks which model writes it (never the chat model) and can make it the default", async ({ page, request }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("tab-summary").click();
  await expect(page.getByTestId("generate-summary")).toContainText("(choose a model)");
  await page.getByTestId("generate-summary").click();
  const dialog = page.getByTestId("summary-model-dialog");
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("summary-model-ok")).toBeDisabled();  // a model must be picked
  await dialog.getByLabel("Model", { exact: true }).selectOption("local:gemma4:e4b");
  await page.getByTestId("summary-model-default").check();
  await page.getByTestId("summary-model-ok").click();
  await expect(page.getByTestId("summary-panel")).toContainText("Gemma 4 E4B · local", { timeout: 15_000 });
  await expect.poll(async () => (await prefs(request)).summary_model).toBe("local:gemma4:e4b");
});

test("without a summary model, transcription identifies speakers and the summary waits for a choice", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 3 } });
  await open(page, "/meetings");
  await page.getByTestId("source-folder").click();
  await page.getByRole("option", { name: /standup\.mp4/ }).click();
  const start = page.getByTestId("start-models");
  await expect(start.getByLabel("Summary", { exact: true })).toContainText("Ask me when it's written");
  await expect(start.getByTestId("estimate-summary")).toContainText("you'll be asked which model writes it");
  await start.getByTestId("start-after").check();
  await page.getByTestId("start-transcription").click();
  await expect(page.getByTestId("followup")).toHaveAttribute("data-state", "needs_model", { timeout: 40_000 });
  await page.getByRole("button", { name: "Open transcript" }).click();
  await page.getByTestId("tab-summary").click();
  await expect(page.getByTestId("summary-needs-model")).toBeVisible();
});

test("New chat starts on the cloud model, whatever was picked before", async ({ page }) => {
  await open(page, "/");
  await pickModel(page, "local:qwen3:8b");
  await expect(page.getByTestId("selected-model")).toContainText("Qwen3 8B");
  await page.getByRole("button", { name: /New chat/ }).click();
  await expect(page.getByTestId("selected-model")).toContainText("GLM-5.3 Flash");
});
