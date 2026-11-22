/**
 * E2E: global search (⌘K) over chats, meetings and memory, web search providers and their errors, and the
 * assistant's search tool.
 */
import { expect, test } from "@playwright/test";
import type { APIRequestContext, Page } from "@playwright/test";
import { ADMIN, API, importExport, open, resetAll, send } from "./helpers";

const mod = process.platform === "darwin" ? "Meta" : "Control";

test.beforeEach(async ({ request }) => {
  await resetAll(request);
  await importExport(request);
});

/** Switches web search on with a provider and (fake) API key. */
async function webOn(request: APIRequestContext, provider = "tavily", key: string | null = "tvly-test-key") {
  await request.put(`${API}/api/search/settings`, { data: { web_enabled: true, provider } });
  if (key) await request.put(`${API}/api/search/keys/${provider}`, { data: { key } });
}

/** Opens global search (⌘K) and searches for `q`. */
async function search(page: Page, q: string) {
  await page.keyboard.press(`${mod}+K`);
  await expect(page.getByTestId("search-dialog")).toBeVisible();
  await page.getByTestId("search-input").fill(q);
}

test("internal-only search: grouped results with links; web is off", async ({ page }) => {
  await open(page);
  await search(page, "rollback plan");
  const chats = page.getByTestId("search-group-chats");
  await expect(chats.getByTestId("search-result").filter({ hasText: "Atlas go-live checklist" })).toHaveAttribute("data-kind", "imported_chat");
  await expect(page.getByTestId("search-group-transcripts")).toContainText("Release 4.2 go/no-go");
  await expect(page.getByTestId("search-group-web")).toHaveCount(0);
  await expect(page.getByTestId("web-status")).toContainText("Web search is off");
  // open a transcript hit: lands on the matching line
  await page.getByTestId("search-group-transcripts").getByTestId("search-result").filter({ hasText: "Release 4.2" }).first().click();
  await expect(page).toHaveURL(/\/meetings\/fx-release-review\?t=\d+/);
  await expect(page.locator("[data-focused]")).toContainText("Rollback plan");
});

test("source and date filters", async ({ page }) => {
  await open(page);
  await search(page, "release");
  await expect(page.getByTestId("search-group-chats")).toBeVisible();
  await page.getByTestId("filter-chats").click();
  await page.getByTestId("filter-memory").click();
  await expect(page.getByTestId("search-group-chats")).toHaveCount(0);
  await expect(page.getByTestId("search-group-transcripts")).toBeVisible();
  await page.getByTestId("filter-chats").click();
  await page.getByTestId("date-range").selectOption("week"); // imported chats are older than a week
  await expect(page.getByTestId("search-group-transcripts")).toBeVisible();
  await expect(page.getByTestId("search-group-chats")).toHaveCount(0);
  await page.getByTestId("filter-memory").click();
  await page.getByTestId("date-range").selectOption("any");
  await expect(page.getByTestId("search-group-memory")).toContainText("release manager");
});

test("web-only and combined search with citations", async ({ page, request }) => {
  await webOn(request);
  await open(page);
  await search(page, "release checklist");
  await expect(page.getByTestId("search-group-web").getByTestId("search-result")).toHaveCount(2);
  await expect(page.getByTestId("search-group-chats")).toBeVisible(); // combined
  await expect(page.getByTestId("search-group-web").getByTestId("citation").first()).toHaveText(/example\.com/);
  for (const s of ["chats", "transcripts", "memory", "connectors"]) await page.getByTestId(`filter-${s}`).click();
  await expect(page.getByTestId("search-group-chats")).toHaveCount(0);
  await expect(page.getByTestId("search-group-web")).toContainText("[1]");
});

test("missing key, invalid key, rate limit and offline fall back to your own data", async ({ page, request }) => {
  await webOn(request, "tavily", null);
  await open(page);
  await search(page, "release checklist");
  await expect(page.getByTestId("web-status")).toContainText("No Tavily API key");
  await expect(page.getByTestId("search-group-transcripts")).toBeVisible();
  await page.keyboard.press("Escape");

  await request.put(`${API}/api/search/keys/tavily`, { data: { key: "bad-key" } });
  await search(page, "release checklist ");
  await expect(page.getByTestId("web-status")).toContainText("rejected the API key");
  await page.keyboard.press("Escape");

  await request.put(`${API}/api/search/keys/tavily`, { data: { key: "tvly-test-key" } });
  await request.post(`${ADMIN}/config`, { data: { web_mode: "rate" } });
  await search(page, "release checklist  ");
  await expect(page.getByTestId("web-status")).toContainText("rate limit");
  await expect(page.getByTestId("search-group-chats")).toBeVisible();
  await page.keyboard.press("Escape");

  await webOn(request, "brave", "brave-test-key"); // nothing listens at the Brave URL in tests
  await search(page, "release checklist   ");
  await expect(page.getByTestId("web-status")).toContainText("you may be offline");
  await expect(page.getByTestId("search-group-transcripts")).toBeVisible();
});

test("the assistant searches your data and answers with a citation", async ({ page }) => {
  await open(page);
  await send(page, "Find the sushi place for lunch");
  await expect(page.getByTestId("assistant-message")).toContainText("Here is what I found: Team lunch ideas");
  const link = page.getByTestId("assistant-message").getByRole("link", { name: "1" });
  await expect(link).toHaveAttribute("href", /\/c\/[0-9a-f]+$/);
});
