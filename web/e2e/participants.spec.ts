/**
 * E2E: the call's participants list. Where each speaker's name came from (tiles, the list, both) and where they
 * disagree; the check against the list at the end of identification (Keep), suggestions for an unnamed speaker;
 * attendees in step with the speakers (add, remove, rename); and the cloud review's spelling fixes in the
 * Corrections tab (Keep, Revert all). Synthetic meeting t-panel (web/e2e/mock_services.py).
 */
import { expect, test } from "@playwright/test";
import { ADMIN, addProvider, open, pickModel, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => {
  await resetAll(request);
  await request.post(`${ADMIN}/add-transcript`, { data: { fixture: "panel" } });
});

test("the participants list: sources, the check, suggestions and attendees", async ({ page }) => {
  await open(page, "/meetings/t-panel");
  await expect(page.getByTestId("participants-info")).toContainText("Participants list: in 24 of 25 frames scanned · 4 people");
  await expect(page.getByTestId("screen-sources")).toContainText("Priya Sharma · tiles 3 · list 9 · both 2");
  await expect(page.getByTestId("screen-conflicts")).toContainText("text reader “Priya Sharma”, participants list “Tomás Rivera”");

  await page.getByTestId("identify-speakers").click();
  const check = page.getByTestId("participants-check");
  const owen = check.locator('[data-speaker="SPEAKER_01"]');
  await expect(owen).toContainText(/Owen\s*→\s*Owen Hart/);
  await expect(owen).toContainText("Owen Hart is the only Owen in the participants list");
  const row = page.locator('[data-testid="speaker-row"][data-speaker="SPEAKER_01"]');
  await expect(row.getByTestId("speaker-name")).toHaveText("Owen Hart");
  await expect(row.getByTestId("from-list")).toBeVisible();
  await owen.getByTestId("participants-keep").click();
  await expect(owen).toContainText("kept");

  // the unnamed speaker gets the listed people nobody has yet
  const third = page.locator('[data-testid="speaker-row"][data-speaker="SPEAKER_02"]');
  await expect(third.getByTestId("speaker-suggestions")).toContainText("Mei Lin Chen");
  await third.getByTestId("speaker-suggestion").filter({ hasText: "Mei Lin Chen" }).click();
  await expect(third.getByTestId("speaker-name")).toHaveText("Mei Lin Chen");

  // attendees: everyone in the list, who spoke, the invited, and your changes
  const attendees = page.getByTestId("attendees");
  await expect(attendees).toContainText("Attendees (4 · 3 spoke)");
  const tomas = attendees.locator('[data-testid="attendee-row"][data-name="Tomás Rivera"]');
  await expect(tomas).toContainText("Didn't speak");
  await expect(page.getByTestId("attendee-invited")).toHaveText("Invited, didn't join: Sam Lee");
  await page.getByTestId("attendee-add-input").fill("Tara Quinn");
  await page.getByTestId("attendee-add").click();
  await expect(attendees.locator('[data-name="Tara Quinn"]')).toContainText("Added by you");
  await tomas.getByTestId("attendee-remove").click();
  await expect(attendees.locator('[data-name="Tomás Rivera"]')).toHaveCount(0);
  await expect(attendees.locator('[data-name="Priya Sharma"] [data-testid="attendee-remove"]')).toHaveCount(0);  // she spoke

  // renaming someone who spoke renames the speaker too
  await attendees.locator('[data-name="Priya Sharma"]').getByTestId("attendee-rename").click();
  await page.getByTestId("attendee-rename-input").fill("Priya S. Sharma");
  await page.getByTestId("attendee-rename-save").click();
  await expect(page.locator('[data-testid="speaker-row"][data-speaker="SPEAKER_00"]').getByTestId("speaker-name")).toHaveText("Priya S. Sharma");
  await expect(attendees.locator('[data-name="Priya S. Sharma"]')).toContainText("Spoke");

  // and the other way round: renaming the speaker renames the attendee
  await page.locator('[data-testid="speaker-row"][data-speaker="SPEAKER_01"]').getByTestId("edit-speaker").click();
  await page.getByTestId("speaker-name-input").fill("Owen J. Hart");
  await page.getByTestId("save-speaker").click();
  await expect(attendees.locator('[data-name="Owen J. Hart"]')).toContainText("Spoke");
  await expect(attendees.locator('[data-name="Owen Hart"]')).toHaveCount(0);
});

test("the cloud review fixes spelling first: Keep and Revert all in Corrections", async ({ page, request }) => {
  await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await request.post(`${ADMIN}/spelling-plan`, { data: { fixes: { Owen: "Owain" } } });
  await open(page, "/meetings/t-panel");
  await pickModel(page, "cloud:anthropic:claude-opus-5-5");
  await page.getByTestId("identify-speakers").click();
  await page.getByTestId("send-review").click();
  await expect(page.getByTestId("review-summary")).toContainText("fixed the spelling in 1 line");
  const systems = ((await (await request.get(`${ADMIN}/cloud-requests`)).json()) as { path: string; body: { system: unknown } }[])
    .filter((r) => r.path === "/v1/messages").map((r) => JSON.stringify(r.body.system));
  expect(systems[0]).toContain("proofread");  // the spelling comes first, then the names
  expect(systems[1]).toContain("review speaker identification");
  await expect(page.getByTestId("segments")).toContainText("Hi, Owain here.");

  await page.getByRole("tab", { name: "Corrections" }).click();
  await expect(page.getByTestId("cloud-spelling")).toContainText("Spelling fixed by Claude Opus 5.5 in the cloud review: 1 line");
  await page.getByTestId("corrections-view-changes").click();
  const first = page.getByTestId("correction-change").first();
  await expect(first).toContainText("misheard name");
  await first.getByTestId("correction-keep").click();
  await expect(first.getByTestId("correction-kept")).toBeVisible();
  await page.getByTestId("cloud-spelling-revert-all").click();
  await expect(page.getByTestId("segments")).toContainText("Hi, Owen here.");
  await expect(page.getByTestId("cloud-spelling")).toHaveCount(0);
});
