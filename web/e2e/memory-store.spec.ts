/**
 * E2E: Settings → Memory: grouped like claude.ai with source and date; search, add, edit and delete facts;
 * topics; export; forgetting a voice; memory used in chats.
 */
import { expect, test } from "@playwright/test";
import { API, importExport, open, pickModel, resetAll, send } from "./helpers";

// The Memory tab: one store shaped like claude.ai memory (synthetic export in e2e/fixtures).
test.beforeEach(async ({ request }) => resetAll(request));

/** Opens Settings → Memory. */
async function openMemory(page: import("@playwright/test").Page) {
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-tab-memory").click();
}

test("imported memory is grouped like claude.ai, with source and date", async ({ page, request }) => {
  await importExport(request, "claude-memory-folder.zip");
  await open(page);
  await openMemory(page);
  await expect(page.getByTestId("memory-group-profile")).toBeVisible();
  await expect(page.getByTestId("memory-group-person")).toBeVisible();
  await expect(page.getByTestId("memory-group-project")).toBeVisible();
  const first = page.getByTestId("memory-group-profile").getByTestId("memory-entry").first();
  await first.getByRole("button", { name: /^Expand/ }).click();
  await expect(first.getByTestId("memory-fact").first()).toContainText("Imported from Claude");
  await expect(first.getByTestId("memory-fact").first()).toContainText(/\d{4}-\d{2}-\d{2}/);
});

test("search, add, edit and delete facts; add a topic; export", async ({ page, request }) => {
  await request.post(`${API}/api/people`, { data: { name: "Owen Hart", role: "QA tester" } });
  await open(page);
  await openMemory(page);
  await page.getByLabel("Entry name").fill("Hiring");
  await page.getByRole("button", { name: "Add", exact: true }).last().click();
  const hiring = page.getByTestId("memory-entry").filter({ hasText: "Hiring" });
  await hiring.getByRole("button", { name: "Expand Hiring" }).click();
  await hiring.getByLabel("New fact for Hiring").fill("Interviews are on Wednesdays");
  await hiring.getByLabel("New fact for Hiring").press("Enter");
  await expect(hiring.getByTestId("memory-fact")).toContainText("Entered by you");
  await hiring.getByLabel("Edit fact: Interviews are on Wednesdays").click();
  await hiring.getByLabel("Edit fact", { exact: true }).fill("Interviews are on Thursdays");
  await hiring.getByRole("button", { name: "Save" }).click();
  await expect(hiring.getByTestId("memory-fact")).toContainText("edited");

  await page.getByLabel("Search memory").fill("thursdays");
  await expect(page.getByTestId("memory-entry")).toHaveCount(1);
  await expect(page.getByTestId("person-row")).toHaveCount(0);
  await page.getByLabel("Search memory").fill("");
  await expect(page.getByTestId("person-row").filter({ hasText: "Owen Hart" })).toContainText("QA tester");

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export ZIP" }).click();
  expect((await download).suggestedFilename()).toMatch(/^quorum-memory-.*\.zip$/);

  await hiring.getByLabel("Delete fact: Interviews are on Thursdays").click();
  await expect(hiring.getByTestId("memory-fact")).toHaveCount(0);
});

test("forget a person's voice", async ({ page, request }) => {
  await request.post(`${API}/api/people`, { data: { name: "Owen Hart" } });
  await request.put(`${API}/api/meetings/transcripts/t-standup/speakers`, { data: { labels: { SPEAKER_01: { name: "Owen Hart", role: "QA" } } } });
  await open(page);
  await openMemory(page);
  const owen = page.getByTestId("person-row").filter({ hasText: "Owen Hart" });
  await expect(owen).toContainText("Voice known");
  await owen.getByRole("button", { name: "Forget Owen Hart's voice" }).click();
  await page.getByRole("button", { name: "Forget voice" }).click();
  await expect(owen).not.toContainText("Voice known");
});

test("chat uses relevant memory and says which facts", async ({ page, request }) => {
  const person = await (await request.post(`${API}/api/people`, { data: { name: "Cian Byrne", role: "Release manager" } })).json();
  await request.post(`${API}/api/memory/entries/${person.id}/facts`, { data: { text: "Runs the release train every second Tuesday." } });
  await open(page);
  await pickModel(page, "local:gemma4:e4b");
  await send(page, "When does Cian Byrne run the release train?");
  const used = page.getByTestId("assistant-message").last().getByTestId("memory-used");
  await expect(used).toContainText("Used 2 facts from memory");
  await used.getByRole("button").click();
  await expect(used).toContainText("Cian Byrne: Release manager");
});
