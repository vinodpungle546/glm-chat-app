/**
 * E2E: finding connectors in the (fake) MCP Registry and adding remote and package connectors, sign-in,
 * test, update and remove.
 */
import { expect, test } from "@playwright/test";
import { ADMIN, open, pickModel, resetAll, send } from "./helpers";

// Settings → Connectors → Find connectors, against the fake MCP Registry and servers
// (tests/fake_mcp.py). The local package never installs: the fake stdio server runs instead.
test.beforeEach(async ({ request }) => resetAll(request));

/** Opens Settings → Connectors. */
async function openConnectors(page: import("@playwright/test").Page) {
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-tab-connectors").click();
}

/** Searches the fake MCP Registry from the Find connectors box. */
async function search(page: import("@playwright/test").Page, q: string) {
  await page.getByLabel("Search connectors").fill(q);
  await page.getByTestId("find-connectors").getByRole("button", { name: "Search" }).click();
}

test("search sends only the query; results show publisher, version and needs", async ({ page, request }) => {
  await open(page);
  await openConnectors(page);
  await search(page, "echo");
  const results = page.getByTestId("directory-result");
  await expect(results).toHaveCount(3);
  const remote = results.filter({ hasText: "Echo Remote" });
  await expect(remote).toContainText("github.com/example");
  await expect(remote).toContainText("1.2.0");
  await expect(remote).toContainText("Official registry");
  await expect(remote).toContainText("Needs: API key");
  const sent = (await (await request.get(`${ADMIN}/mcp`)).json()).searches;
  expect(sent).toEqual([{ search: "echo", limit: "20" }]);

  await search(page, "slow-down");
  await expect(page.getByTestId("directory-error")).toContainText("limiting requests");
});

test("add a remote connector and a local package, use both, then disable and remove", async ({ page, request }) => {
  await open(page);
  await openConnectors(page);
  await search(page, "echo");
  await page.getByTestId("directory-result").filter({ hasText: "Echo Remote" }).getByRole("button", { name: "Details" }).click();
  const details = page.getByTestId("connector-details");
  await details.getByLabel("Authorization *").fill("Bearer key-123");
  await page.getByRole("button", { name: "Add connector" }).click();
  const rows = page.getByTestId("connector-row");
  await expect(rows.filter({ hasText: "Echo Remote" }).getByTestId("connector-tools")).toHaveText("Tools: echo, add");

  await page.getByTestId("directory-result").filter({ hasText: "Echo Local" }).getByRole("button", { name: "Details" }).click();
  await expect(details.getByTestId("stdio-command")).toHaveText("npx -y @example/echo-mcp@2.0.1");
  await expect(details).toContainText("This runs third-party code on your Mac");
  await details.getByLabel("ECHO_TOKEN *").fill("s3cret");
  const install = page.getByRole("button", { name: "Install and add" });
  await expect(install).toBeDisabled();
  await details.getByLabel("Confirm running this package").check();
  await install.click();
  const local = rows.filter({ hasText: "Echo Local" });
  await expect(local.getByTestId("connector-tools")).toHaveText("Tools: echo, whoami");
  await expect(local).toContainText("Local package");
  await expect(page.getByTestId("directory-result").filter({ hasText: "Echo Local" })).toContainText("Added");

  await page.keyboard.press("Escape");
  await pickModel(page, "local:gemma4:e4b");
  await send(page, "Use Echo Remote to echo hello remote");
  await expect(page.getByTestId("assistant-message").last()).toContainText("The connector answered: echo: hello remote");
  expect((await (await request.get(`${ADMIN}/mcp`)).json()).calls.at(-1)).toMatchObject({ name: "echo", arguments: { text: "hello remote" } });
  await send(page, "Use Echo Local to echo hello local");
  await expect(page.getByTestId("assistant-message").last()).toContainText("echo: hello local [token set]");

  await openConnectors(page);
  await local.getByRole("switch").click();
  await expect(local).toContainText("Off");
  await local.getByRole("button", { name: "Remove Echo Local" }).click();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(rows.filter({ hasText: "Echo Local" })).toHaveCount(0);
});
