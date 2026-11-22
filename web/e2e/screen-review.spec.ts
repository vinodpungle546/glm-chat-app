/**
 * E2E: names read from video, the cloud review of speaker names (Keep / Revert), and notes using reviewed
 * names.
 */
import { expect, test } from "@playwright/test";
import { ADMIN, API, addProvider, chooseSummaryModel, open, pickModel, resetAll } from "./helpers";

// A synthetic screen-recorded call (t-video): names were read from the video tiles for SPEAKER_00 and
// SPEAKER_02; SPEAKER_01 was sharing their screen. The cloud model is the fake Claude API.
test.beforeEach(async ({ request }) => resetAll(request));

test("on-screen names explain the guess and teach the voice", async ({ page }) => {
  await open(page, "/meetings/t-video");
  await expect(page.getByTestId("screen-info")).toContainText("7 of 10 frames");
  await page.getByTestId("identify-speakers").click();
  const priya = page.locator('[data-speaker="SPEAKER_00"]');
  await expect(priya.getByTestId("speaker-name")).toHaveText("Priya Sharma");
  await expect(priya.getByTestId("speaker-why")).toHaveText("name on the active tile at 0:01 (seen 4×)");
  await expect(priya.getByTestId("confidence")).toHaveText(/High · \d+%/);

  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-tab-memory").click();
  const person = page.getByTestId("person-row").filter({ hasText: "Priya Sharma" });
  await expect(person).toContainText("Voice known · 1 fingerprint");
  await expect(person).toContainText("On screen: Priya Sharma");
});

test("cloud review: asks in Auto, shows each change, Keep and Revert", async ({ page, request }) => {
  await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await request.post(`${ADMIN}/review-plan`, { data: { changes: {
    SPEAKER_01: { name: "Daniel Okafor", role: "QA lead", lines: [2], reason: "Introduces himself as Daniel." },
    SPEAKER_02: { name: "Mei Lin Chen", role: "Release engineer", lines: [4], reason: "Deploys the refund fix." } } } });
  await open(page, "/meetings/t-video");
  await pickModel(page, "cloud:anthropic:claude-opus-5-5");
  await page.getByTestId("identify-speakers").click();
  await expect(page.getByText("Send speaker names and the transcript to Claude Opus 5.5 for review?")).toBeVisible();
  expect((await (await request.get(`${ADMIN}/cloud-requests`)).json()).filter((r: { path: string }) => r.path === "/v1/messages")).toHaveLength(0);
  await page.getByTestId("send-review").click();

  const summary = page.getByTestId("review-summary");
  await expect(summary).toContainText("Changed by Claude Opus 5.5");
  const daniel = summary.locator('[data-speaker="SPEAKER_01"]');
  await expect(daniel).toContainText("Daniel Okafor");
  await expect(daniel).toContainText("Introduces himself as Daniel.");
  await expect(daniel.getByRole("button", { name: "Show line 2" })).toBeVisible();
  await expect(page.getByText("Claude Opus 5.5 changed 2 speaker names")).toBeVisible();
  const row = page.locator('[data-testid="speaker-row"][data-speaker="SPEAKER_01"]');
  await expect(row).toContainText("Changed by Claude Opus 5.5");
  await expect(row.getByTestId("speaker-why")).toHaveText("Claude Opus 5.5: Introduces himself as Daniel.");

  // what was sent: transcript and suggestions, never voice fingerprints
  const sent = JSON.stringify((await (await request.get(`${ADMIN}/cloud-requests`)).json()).at(-1).body);
  expect(sent).toContain("Hi, Daniel here");
  expect(sent).not.toContain("speaker_embeddings");
  expect(sent).not.toContain("0.9, 0.1");

  await daniel.getByRole("button", { name: "Keep" }).click();
  await expect(daniel).toContainText("kept");
  await summary.locator('[data-speaker="SPEAKER_02"]').getByRole("button", { name: "Revert" }).click();
  await expect(page.locator('[data-testid="speaker-row"][data-speaker="SPEAKER_02"]').getByTestId("speaker-name")).toHaveText("Mei Lin Chen");
  await expect(summary.locator('[data-speaker="SPEAKER_02"]')).toContainText("reverted");

  const people = await (await request.get(`${API}/api/people`)).json();
  const kept = people.find((p: { name: string }) => p.name === "Daniel Okafor");
  expect(kept.voiceprint_count).toBe(1);
  expect(kept.role).toBe("QA lead");
});

test("Revert all, and a model that agrees", async ({ page, request }) => {
  await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await request.put(`${API}/api/preferences`, { data: { confirm_cloud_sensitive: false } });
  await request.post(`${ADMIN}/review-plan`, { data: { changes: {
    SPEAKER_00: { name: "Priya Sharma", role: "Engineering manager", lines: [1, 3], reason: "Runs the sync." },
    SPEAKER_01: { name: "Daniel Okafor", role: "QA lead", lines: [2] } } } });
  await open(page, "/meetings/t-video");
  await pickModel(page, "cloud:anthropic:claude-opus-5-5");
  await page.getByTestId("identify-speakers").click();
  const summary = page.getByTestId("review-summary");
  await expect(summary.getByTestId("review-change")).toHaveCount(2);
  await summary.getByRole("button", { name: "Revert all" }).click();
  await expect(summary.getByText("reverted")).toHaveCount(2);
  await expect(page.locator('[data-testid="speaker-row"][data-speaker="SPEAKER_01"]')).not.toContainText("Daniel");

  await request.post(`${ADMIN}/review-plan`, { data: { changes: {} } });
  await page.getByTestId("identify-speakers").click();
  await expect(page.getByTestId("review-summary")).toHaveText("Claude Opus 5.5 agreed with all local names.");
});

test("notes use names kept from the review and memory", async ({ page, request }) => {
  await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await request.put(`${API}/api/preferences`, { data: { confirm_cloud_sensitive: false } });
  await request.post(`${API}/api/memory/entries`, { data: { kind: "project", name: "Atlas", description: "Payments rebuild" } });
  await request.post(`${ADMIN}/review-plan`, { data: { changes: {
    SPEAKER_01: { name: "Daniel Okafor", role: "QA lead", lines: [2], reason: "Introduces himself as Daniel." } } } });
  await open(page, "/meetings/t-video");
  await pickModel(page, "cloud:anthropic:claude-opus-5-5");
  await page.getByTestId("identify-speakers").click();
  await page.getByTestId("review-summary").getByRole("button", { name: "Keep" }).click();
  await expect(page.getByTestId("review-summary")).toContainText("kept");

  await pickModel(page, "local:gemma4:e4b");
  await page.getByTestId("tab-summary").click();
  await page.getByTestId("generate-summary").click();
  await chooseSummaryModel(page);
  const notes = page.getByTestId("notes");
  await expect(notes).toContainText("Daniel Okafor (QA lead)", { timeout: 20_000 });
  await expect(notes.getByRole("cell", { name: "Daniel Okafor" }).first()).toBeVisible();  // "Daniel, please…" → full name
  await expect(notes).toContainText("Atlas");
  await expect(notes).not.toContainText("Zed");
});

