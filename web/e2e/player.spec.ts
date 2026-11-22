/**
 * E2E: playing a meeting back on its page. A line's time plays the recording from there and the line being heard is
 * marked; speed and pause; a call Quorum recorded has separate Call and You volumes. The fake transcriber serves
 * synthetic tones as WAV (the test browser has no H.264, so no video here).
 */
import { expect, test } from "@playwright/test";
import { ADMIN, API, open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("a line's time plays the meeting from there; pause and speed", async ({ page, request }) => {
  const t = await (await request.get(`${API}/api/meetings/transcripts/t-standup`)).json();
  const line = 2;
  const start: number = t.segments[line].start;
  await open(page, "/meetings/t-standup");
  const player = page.getByTestId("player");
  await expect(player).toBeVisible();
  await expect(player.getByTestId("player-volume-audio")).toBeVisible();

  await page.locator(`[data-seg="${line}"]`).getByTestId("seg-play").click();
  await expect(player).toHaveAttribute("data-playing", "true");
  await expect.poll(async () => Number(await player.getAttribute("data-time"))).toBeGreaterThan(start);
  expect(Number(await player.getAttribute("data-time"))).toBeLessThan(start + 5);
  await expect(page.locator(`[data-seg="${line}"]`)).toHaveAttribute("data-heard", "true");

  await player.getByTestId("player-rate").click();
  await expect(player.getByTestId("player-rate")).toContainText("1.25×");
  expect(await page.evaluate(() => (document.querySelector("[data-testid=player-audio-audio]") as HTMLAudioElement).playbackRate)).toBe(1.25);
  await player.getByTestId("player-play").click();
  await expect(player).not.toHaveAttribute("data-playing", "true");
});

test("a recorded call plays the call and your microphone with their own volume", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 1 } });
  await request.post(`${API}/api/meetings/record/start`, { data: { source: { kind: "audio" }, title: "Playback call" } });
  const stopped = await (await request.post(`${API}/api/meetings/record/stop`, { data: { transcribe: true } })).json();
  await expect.poll(async () => (await (await request.get(`${API}/api/meetings/jobs`)).json())
    .find((j: { id: string }) => j.id === stopped.job.id)?.status, { timeout: 15_000 }).toBe("done");
  const list = await (await request.get(`${API}/api/meetings/transcripts`)).json();
  const call = list.find((t: { title: string }) => t.title.startsWith("Playback call"));

  await open(page, `/meetings/${call.id}`);
  const player = page.getByTestId("player");
  await expect(player.getByTestId("player-volume-call")).toBeVisible();
  await expect(player.getByTestId("player-volume-mic")).toBeVisible();
  await player.getByTestId("player-volume-mic").fill("0");
  await player.getByTestId("player-play").click();
  await expect(player).toHaveAttribute("data-playing", "true");
  const volumes = await page.evaluate(() => ["call", "mic"].map((p) => {
    const el = document.querySelector(`[data-testid=player-audio-${p}]`) as HTMLAudioElement;
    return { volume: el.volume, paused: el.paused };
  }));
  expect(volumes).toEqual([{ volume: 1, paused: false }, { volume: 0, paused: false }]);
  await player.getByTestId("player-play").click();
});
