/**
 * E2E: meeting notes for every meeting type, type override, detail levels, sections, inline edits,
 * timestamp links and all exports.
 */
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { chooseSummaryModel, open, pickModel, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

const TYPES: [string, string, string][] = [
  ["general", "General meeting", "Agenda & discussion"],
  ["standup", "Daily stand-up", "Stand-up updates"],
  ["sprint-planning", "Sprint planning", "Committed stories"],
  ["retrospective", "Retrospective", "What went well"],
  ["status-review", "Status / steering review", "Overall status"],
  ["client-call", "Client call", "Commitments made by our side"],
  ["release-review", "Release / go-live review", "Go / no-go"],
  ["incident", "Incident / postmortem", "Root cause"],
  ["one-on-one", "1:1", "Discussion points"],
  ["interview", "Interview", "Competencies assessed"],
  ["brainstorm", "Brainstorm / workshop", "Ideas by theme"],
];

/** Opens a fixture meeting and generates its notes with Gemma (answering the model question). */
async function generate(page: Page, id: string) {
  await open(page, `/meetings/fx-${id}`);
  await page.getByTestId("tab-summary").click();
  await page.getByTestId("generate-summary").click();
  await chooseSummaryModel(page);
}

/** The rendered notes. */
const notes = (page: Page) => page.getByTestId("notes");

test("notes for every meeting type (Gemma 4 E4B), with the type detected automatically", async ({ page }) => {
  test.setTimeout(150_000);
  for (const [id, label, section] of TYPES) {
    await generate(page, id);
    await expect(notes(page)).toContainText(`Meeting type: ${label}`, { timeout: 20_000 });
    await expect(notes(page).getByRole("heading", { name: section })).toBeVisible();
    for (const core of ["Summary", "Decisions", "Action items"]) await expect(notes(page).getByRole("heading", { name: core, exact: true })).toBeVisible();
    await expect(notes(page)).toContainText("Notes written by gemma4:e4b");
    await expect(page.getByTestId("summary-panel")).toContainText("Gemma 4 E4B · local");
  }
});

test("auto-detect can be overridden before and after generation", async ({ page }) => {
  await open(page, "/meetings/fx-standup");
  await page.getByTestId("tab-summary").click();
  await expect(page.getByTestId("meeting-type")).toContainText("Auto-detect (Daily stand-up)");
  await page.getByTestId("meeting-type").selectOption("retrospective");
  await page.getByTestId("generate-summary").click();
  await chooseSummaryModel(page);
  await expect(notes(page)).toContainText("Meeting type: Retrospective");
  await expect(notes(page).getByRole("heading", { name: "What went well" })).toBeVisible();
  await page.getByTestId("meeting-type").selectOption("standup");
  await expect(notes(page)).toContainText("Meeting type: Daily stand-up");
  await expect(notes(page).getByRole("heading", { name: "Stand-up updates" })).toBeVisible();
});

test("detail levels and section toggles", async ({ page }) => {
  await generate(page, "release-review");
  await expect(notes(page).getByRole("heading", { name: "Risks, issues & blockers" })).toBeVisible();
  await page.getByTestId("detail-brief").click();
  await expect(notes(page).getByRole("heading", { name: "Risks, issues & blockers" })).toHaveCount(0);
  await expect(notes(page).getByRole("heading", { name: "Action items" })).toBeVisible();
  await page.getByTestId("detail-detailed").click();
  await expect(notes(page).getByRole("heading", { name: "Appendix" })).toBeVisible();
  await page.getByTestId("sections-button").click();
  await page.getByTestId("sections-popover").getByLabel("Appendix (quotes, transcript link)").uncheck();
  await expect(notes(page).getByRole("heading", { name: "Appendix" })).toHaveCount(0);
  await expect(notes(page).getByRole("heading", { name: "Decisions" })).toBeVisible();
});

test("inline edits are saved with the notes", async ({ page }) => {
  await generate(page, "release-review");
  const actions = page.getByTestId("notes-section").filter({ has: page.getByRole("heading", { name: "Action items" }) });
  await actions.hover();
  await actions.getByTestId("edit-section").click();
  const editor = page.getByTestId("section-editor");
  await editor.fill((await editor.inputValue()).replace("Finish the release notes", "Publish the release notes"));
  await page.getByTestId("save-section").click();
  await expect(notes(page)).toContainText("Publish the release notes");
  await page.reload();
  await page.getByTestId("tab-summary").click();
  await expect(notes(page)).toContainText("Publish the release notes");
  await expect(page.getByTestId("summary-panel")).toContainText("Edited");
});

test("timestamps link back to the transcript", async ({ page }) => {
  await generate(page, "release-review");
  await notes(page).getByRole("link", { name: "00:02:00" }).first().click();
  await expect(page.locator("[data-focused]")).toContainText("smoke test");
});

test("every export format, copy to clipboard, follow-up email and action items CSV", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await generate(page, "release-review");
  await expect(notes(page)).toContainText("Meeting type: Release / go-live review");
  const fs = await import("node:fs");
  for (const [format, ext] of [["md", ".md"], ["docx", ".docx"], ["pdf", ".pdf"], ["html", ".html"], ["csv", ".csv"]]) {
    await page.getByTestId("export-menu").click();
    const [download] = await Promise.all([page.waitForEvent("download"), page.getByTestId(`export-${format}`).click()]);
    expect(download.suggestedFilename()).toMatch(new RegExp(`\\${ext}$`));
    const body = fs.readFileSync((await download.path())!);
    expect(body.length).toBeGreaterThan(100);
    if (format === "csv") {
      const csv = body.toString();
      expect(csv.split(/\r?\n/)[0]).toBe("#,Action,Owner,Due date,Priority,Timestamp,Link");
      expect(csv).toContain("Finish the release notes,Cian,18 Nov 2026,High,00:01:00");
    }
    if (format === "pdf") expect(body.subarray(0, 4).toString()).toBe("%PDF");
    if (format === "md") expect(body.toString()).toContain("# Release 4.2 go/no-go");
  }
  await page.getByTestId("copy-notes").click();
  await expect(page.getByText("Copied")).toBeVisible();
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  expect(clip).toContain("Release 4.2 go/no-go");
  expect(clip).toContain("Finish the release notes — Cian");
  await page.getByTestId("draft-email").click();
  const draft = page.getByTestId("email-draft");
  await expect(draft.getByLabel("Subject")).toHaveValue("Follow-up: Release 4.2 go/no-go (18 Nov 2026)");
  await expect(draft.getByLabel("Email body")).toHaveValue(/• Finish the release notes — Cian — 18 Nov 2026/);
});

test("meeting notes with the cloud model ask first, then use it", async ({ page }) => {
  await open(page, "/meetings/fx-client-call");
  await pickModel(page, "cloud:gaia");
  await page.getByTestId("tab-summary").click();
  await page.getByTestId("generate-summary").click();
  await chooseSummaryModel(page, "cloud:gaia");
  await expect(page.getByText("Send this meeting to the cloud?")).toBeVisible();
  await page.getByTestId("summary-cloud").click();
  await expect(notes(page)).toContainText("Notes written by GLM-5.3 Flash (cloud)");
  await expect(notes(page)).toContainText("Meeting type: Client call");
  await expect(page.getByTestId("summary-panel")).toContainText("GLM-5.3 Flash · cloud");
});

test("answering 'Use local model' writes the notes locally instead of asking again", async ({ page }) => {
  await open(page, "/meetings/fx-client-call");
  await page.getByTestId("tab-summary").click();
  await page.getByTestId("generate-summary").click();
  await chooseSummaryModel(page, "cloud:gaia");
  await expect(page.getByText("Send this meeting to the cloud?")).toBeVisible();
  await expect(page.getByTestId("summary-local")).toContainText("gemma4:e4b");
  await page.getByTestId("summary-local").click();
  await expect(page.getByText("Send this meeting to the cloud?")).toBeHidden();
  await expect(notes(page)).toContainText("Meeting type: Client call");
  await expect(page.getByTestId("summary-panel")).toContainText("· local");
});
