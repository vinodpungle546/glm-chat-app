/**
 * E2E: Settings → Models: adding Claude (fake API), testing the key, choosing models, chatting with Claude
 * and OpenAI models, naming the cloud model before sending sensitive text, and cloud errors.
 */
import { expect, test } from "@playwright/test";
import { ADMIN, API, addProvider, open, pickModel, resetAll, send } from "./helpers";

// Claude and OpenAI providers against fake APIs (tests/fake_cloud.py); keys stay in the secret store.
test.beforeEach(async ({ request }) => resetAll(request));

test("add Claude in Settings → Models, test the key and choose models", async ({ page, request }) => {
  await open(page);
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-tab-providers").click();
  const add = page.getByTestId("add-provider");
  await add.getByLabel("API key").fill("sk-wrong");
  await add.getByRole("button", { name: "Add provider" }).click();
  const card = page.getByTestId("provider-anthropic");
  await expect(card).toContainText("The API key was rejected.");

  await card.getByLabel("New API key for Anthropic (Claude)").fill("sk-ant-test");
  await card.getByRole("button", { name: "Save key" }).click();
  await card.getByRole("button", { name: "Test connection" }).click();
  await expect(card.getByTestId("provider-test-result")).toContainText("Connected · 3 models");
  const models = card.getByTestId("provider-models");
  await expect(models.getByLabel("Claude Opus 5.5")).toBeChecked();
  await models.getByLabel("Claude Haiku 5.5").uncheck();
  await expect(models.getByLabel("Claude Haiku 5.5")).not.toBeChecked();

  const prefs = JSON.stringify(await (await request.get(`${API}/api/preferences`)).json());
  const providers = JSON.stringify(await (await request.get(`${API}/api/providers`)).json());
  expect(prefs + providers).not.toContain("sk-ant-test");
  await expect(card).toContainText("Key ••••test");

  await page.keyboard.press("Escape");
  await page.getByTestId("model-picker").click();
  await expect(page.getByTestId("model-option-cloud:anthropic:claude-opus-5-5")).toBeVisible();
  await expect(page.getByTestId("model-option-cloud:anthropic:claude-haiku-5-5")).toHaveCount(0);
  await expect(page.getByTestId("model-option-cloud:gaia")).toBeVisible();  // GAIA stays built in
});

test("chat with a Claude model and an OpenAI model", async ({ page, request }) => {
  await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await addProvider(request, "openai", ["gpt-5.5"]);
  await open(page);
  await pickModel(page, "cloud:anthropic:claude-opus-5-5");
  await send(page, "What is on the agenda?");
  const answer = page.getByTestId("assistant-message").last();
  await expect(answer).toContainText("Fake reply about: What is on the agenda?");
  await expect(answer.getByTestId("message-model")).toHaveText(/Claude Opus 5\.5 · cloud/);

  await pickModel(page, "cloud:openai:gpt-5.5");
  await send(page, "And the budget?");
  await expect(page.getByTestId("assistant-message").last()).toContainText("Fake reply about: And the budget?");
  await expect(page.getByTestId("assistant-message").last().getByTestId("message-model")).toHaveText(/gpt-5\.5 · cloud/);
  const sent = await (await request.get(`${ADMIN}/cloud-requests`)).json();
  expect(sent.map((r: { api: string }) => r.api)).toEqual(expect.arrayContaining(["anthropic", "openai"]));
});

test("sensitive text in Auto mode names the cloud model before sending", async ({ page, request }) => {
  await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await open(page);
  await pickModel(page, "cloud:anthropic:claude-opus-5-5");
  await send(page, "The salary bands are confidential.");
  await expect(page.getByTestId("send-cloud")).toHaveText(/Send to Claude Opus 5\.5 anyway/);
  await expect(page.getByRole("alertdialog")).toContainText("Anthropic (Claude)");
  expect((await (await request.get(`${ADMIN}/cloud-requests`)).json()).filter((r: { path: string }) => r.path === "/v1/messages")).toHaveLength(0);
  await page.getByTestId("send-cloud").click();
  await expect(page.getByTestId("assistant-message").last()).toContainText("Fake reply about");
});

test("a cloud error is shown clearly", async ({ page, request }) => {
  const added = await addProvider(request, "anthropic", ["claude-opus-5-5"]);
  await request.patch(`${API}/api/providers/${added.id}`, { data: { manual_model: "claude-limit" } });
  await request.put(`${API}/api/preferences`, { data: { confirm_cloud_sensitive: false } });
  await open(page);
  await pickModel(page, "cloud:anthropic:claude-limit");
  await send(page, "Hello");
  await expect(page.getByTestId("chat-error")).toContainText("The cloud model couldn't answer");
  await expect(page.getByTestId("chat-error")).toContainText("rate limit");
});
