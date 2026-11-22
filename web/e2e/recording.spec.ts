/**
 * E2E: recording a call (fake Quorum Recorder in mock_services): choosing what to record, the top-bar indicator on
 * every page, Stop → transcription → your microphone confirmed, permission help, and the live transcript (Copy,
 * Ask in chat that stays up to date, Actions so far, swapped for the final transcript).
 */
import { expect, test } from "@playwright/test";
import { ADMIN, API, open, pickModel, resetAll, send } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("record a call from the meeting app; the indicator shows everywhere; Stop transcribes with you confirmed", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 3 } });
  await open(page, "/meetings");
  await page.getByTestId("source-record").click();
  const tab = page.getByTestId("record-tab");
  // Calls first, then remote desktops; the meeting app is preselected.
  await expect(tab.getByTestId("record-app")).toHaveValue("us.zoom.xos");
  await expect(tab.getByTestId("record-app").locator("optgroup")).toHaveText([/zoom\.us/, /Windows App/, /TextEdit/]);
  await tab.getByTestId("record-kind-display").check();
  await expect(tab.getByTestId("record-display")).toBeVisible(); // two displays: pick one
  await tab.getByTestId("record-kind-app").check();
  await tab.getByLabel("Title").fill("Release sync");
  await tab.getByTestId("record-mic").selectOption("airpods");
  await expect(tab.getByText(/Bluetooth headset's microphone switches it to call mode/)).toBeVisible();
  await tab.getByTestId("record-start").click();

  const bar = page.getByTestId("recording-bar");
  await expect(bar).toBeVisible();
  await expect(bar.getByTestId("record-meters")).toBeVisible();
  await expect(page.getByTestId("recording-indicator")).toBeVisible();
  await page.getByTestId("nav-chat").click();
  await expect(page.getByTestId("recording-indicator")).toBeVisible(); // on every page
  await page.getByTestId("recording-indicator").getByRole("button", { name: /Recording/ }).click();
  await expect(page).toHaveURL(/\/meetings$/);

  await page.getByTestId("record-stop").click();
  const panel = page.getByTestId("job-panel");
  await expect(panel).toHaveAttribute("data-status", "done", { timeout: 15_000 });
  await expect(page.getByTestId("recording-indicator")).toHaveCount(0);
  const last = await (await request.get(`${API}/api/meetings/record/sources`)).json();
  expect(last.last.source).toEqual({ kind: "app", id: "us.zoom.xos" });
  expect(last.last.mic).toBe("airpods");

  // The transcript of a call Quorum recorded: you are confirmed on your microphone track.
  const list = await (await request.get(`${API}/api/meetings/transcripts`)).json();
  const call = list.find((t: { title: string }) => t.title.startsWith("Release sync"));
  await open(page, `/meetings/${call.id}`);
  await page.getByTestId("identify-speakers").click();
  await expect(page.getByTestId("confirmed-mic")).toHaveText("Confirmed: your microphone", { timeout: 15_000 });
});

test("without screen access, macOS's picker chooses the window; closing it records nothing", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { record_picker: true, record_pick: "cancel" } });
  await open(page, "/meetings");
  await page.getByTestId("source-record").click();
  const tab = page.getByTestId("record-tab");
  await expect(tab.getByTestId("record-picker-note")).toContainText("macOS shows its own picker");
  await expect(tab.getByTestId("record-app")).toHaveCount(0);  // no app list: the picker chooses
  await tab.getByTestId("record-kind-display").check();
  await expect(tab.getByTestId("record-display")).toHaveCount(0);
  await tab.getByTestId("record-kind-app").check();
  await tab.getByTestId("record-start").click();
  await expect(page.getByTestId("record-cancelled")).toContainText("No window or screen was chosen");

  await request.post(`${ADMIN}/config`, { data: { record_pick: "zoom" } });
  await page.getByRole("button", { name: "Try again" }).click();
  await page.getByTestId("record-tab").getByTestId("record-start").click();
  await expect(page.getByTestId("recording-bar")).toBeVisible();
  await page.getByTestId("record-stop").click();
  await expect(page.getByTestId("job-panel")).toBeVisible({ timeout: 15_000 });
});

test("when macOS hasn't allowed recording yet, the page says where to allow it", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { record_error: "permission_screen" } });
  await open(page, "/meetings");
  await page.getByTestId("source-record").click();
  await expect(page.getByTestId("record-permission")).toContainText("Screen & System Audio Recording");
  await expect(page.getByTestId("record-permission")).toContainText("Quorum Recorder");
  await request.post(`${ADMIN}/config`, { data: { record_error: null } });
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("record-start")).toBeVisible();
});

test("live transcript: lines arrive, Copy, Ask in chat stays up to date, Actions so far, then the final transcript", async ({ page, request, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 2, live_line_seconds: 2 } });
  await open(page, "/meetings");
  await page.getByTestId("source-record").click();
  await page.getByLabel("Title").fill("Release sync");
  await page.getByRole("switch", { name: "Transcribe during the meeting" }).click();
  await page.getByTestId("record-start").click();

  const live = page.getByTestId("live-panel");
  await expect(live).toBeVisible();
  await expect(live.getByTestId("live-lines")).toContainText("Priya Sharma: Release is on Friday, the 7th.");
  await expect(live.getByTestId("live-lines")).toContainText("You: Owen owns the rollback plan.");
  await live.getByTestId("live-copy").click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain("You: Owen owns the rollback plan.");

  // Ask in chat: the draft is attached, marked live, and refreshed before each message.
  await live.getByTestId("live-ask").click();
  await expect(page).toHaveURL(/\/c\//);
  await expect(page.getByTestId("live-attachment")).toBeVisible();
  const sid = page.url().split("/c/")[1];
  const before = (await (await request.get(`${API}/api/sessions/${sid}`)).json()).attachments[0].chars;
  await page.waitForTimeout(4500); // more of the meeting is said
  await pickModel(page, "local:gemma4:e4b"); // local: the transcript stays on this Mac (no cloud question)
  await send(page, "Who owns the rollback plan?");
  await expect(page.getByTestId("assistant-message").first()).toContainText("Echo from gemma4:e4b", { timeout: 15_000 });
  const after = (await (await request.get(`${API}/api/sessions/${sid}`)).json()).attachments[0];
  expect(after.chars).toBeGreaterThan(before);
  expect(after.live).toBeTruthy();

  // Actions so far: a new chat with the question already sent.
  await page.getByTestId("indicator-live").click();
  await page.getByTestId("live-actions").click();
  await expect(page.getByTestId("messages")).toContainText("List the decisions and action items so far");
  await expect(page.getByTestId("assistant-message").first()).toBeVisible({ timeout: 15_000 });

  // Stop: once the final transcript exists, the chat's attachment becomes it.
  await open(page, "/meetings");
  await page.getByTestId("record-stop").click();
  await expect(page.getByTestId("job-panel")).toHaveAttribute("data-status", "done", { timeout: 15_000 });
  await open(page, `/c/${sid}`);
  await send(page, "And now?");
  await expect(page.getByTestId("assistant-message").nth(1)).toBeVisible({ timeout: 15_000 });
  const final = (await (await request.get(`${API}/api/sessions/${sid}`)).json()).attachments[0];
  expect(final.live).toBeNull();
  expect(final.transcript_id).toBeTruthy();
});
