/**
 * E2E: copying and downloading a meeting transcript as shown on the page (speaker names, corrected words), in
 * plain text, with times, Markdown and subtitles. Synthetic meeting only.
 */
import { expect, test } from "@playwright/test";
import { API, open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("copy the transcript in three formats; download as text and subtitles", async ({ page, context, request }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const t = await (await request.get(`${API}/api/meetings/transcripts/t-standup`)).json();
  const first = t.segments[0];
  const name = t.display_names[first.speaker] || first.speaker;
  await open(page, "/meetings/t-standup");

  await page.getByTestId("copy-transcript").click();
  await page.getByTestId("copy-plain").click();
  await expect(page.getByText("Transcript copied")).toBeVisible();
  let clip = await page.evaluate(() => navigator.clipboard.readText());
  expect(clip.startsWith(`${name}: ${first.text.trim()}`)).toBe(true);

  await page.getByTestId("copy-transcript").click();
  await page.getByTestId("copy-timed").click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toMatch(/^\[0:00\] /);
  await page.getByTestId("copy-transcript").click();
  await page.getByTestId("copy-markdown").click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain(`# ${t.title}`);

  // Speaker names as shown: rename one, and the copy follows.
  await request.put(`${API}/api/meetings/transcripts/t-standup/speakers`, { data: { labels: { [first.speaker]: { name: "Priya Sharma" } } } });
  await page.reload();
  await page.getByTestId("copy-transcript").click();
  await page.getByTestId("copy-plain").click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toMatch(/^Priya Sharma: /);

  await page.getByTestId("download-transcript").click();
  const [vtt] = await Promise.all([page.waitForEvent("download"), page.getByTestId("download-vtt").click()]);
  expect(vtt.suggestedFilename()).toBe(`${t.title}.vtt`);
  const body = await (await vtt.createReadStream()).toArray();
  clip = Buffer.concat(body as Buffer[]).toString("utf8");
  expect(clip.startsWith("WEBVTT\n\n00:00:00.000 --> ")).toBe(true);
  expect(clip).toContain("Priya Sharma: ");
  await page.getByTestId("download-transcript").click();
  const [txt] = await Promise.all([page.waitForEvent("download"), page.getByTestId("download-timed").click()]);
  expect(txt.suggestedFilename()).toBe(`${t.title}.txt`);
});
