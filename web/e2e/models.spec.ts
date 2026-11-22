/**
 * E2E: the model picker (local and cloud models, status badges), answering locally, switching to cloud
 * mid-chat, and remembering the selection.
 */
import { expect, test } from "@playwright/test";
import { open, pickModel, resetAll, send, setOllama } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("model picker groups Local and Cloud, including Gemma 4 E4B, with status badges", async ({ page, request }) => {
  await setOllama(request, { installed: ["gemma4:e4b"] });
  await open(page);
  await expect(page.getByTestId("selected-model")).toHaveText("Gemma 4 E4B (Local)");
  await page.getByTestId("model-picker").click();
  const menu = page.getByRole("menu");
  await expect(menu.getByText("Local", { exact: true })).toBeVisible();
  await expect(menu.getByText("Cloud", { exact: true })).toBeVisible();
  await expect(page.getByTestId("model-option-local:gemma4:e4b")).toContainText("Gemma 4 E4B (Local)");
  await expect(page.getByTestId("status-local:gemma4:e4b")).toHaveText("Ready");
  await expect(page.getByTestId("status-local:qwen3:8b")).toHaveText("Not downloaded");
  await expect(page.getByTestId("model-option-cloud:gaia")).toContainText("GLM-5.3 Flash");
});

test("status badges show Ollama not running", async ({ page, request }) => {
  await setOllama(request, { running: false });
  await open(page);
  await page.getByTestId("model-picker").click();
  await expect(page.getByTestId("status-local:gemma4:e4b")).toHaveText("Ollama not running");
  await expect(page.getByTestId("status-local:qwen3:8b")).toHaveText("Ollama not running");
  await page.keyboard.press("Escape");
  await expect(page.getByText("Ollama isn't running")).toBeVisible();
  await expect(page.getByTestId("send")).toBeDisabled();
  await setOllama(request, { running: true });
});

test("selecting a local model answers locally, and switching to cloud mid-session works", async ({ page }) => {
  await open(page);
  await send(page, "Hello there");
  const first = page.getByTestId("assistant-message").first();
  await expect(first).toContainText("Echo from gemma4:e4b: Hello there");
  await expect(first.getByTestId("message-model")).toHaveText("Gemma 4 E4B · local");
  await expect(page).toHaveURL(/\/c\/[0-9a-f]+/);

  await pickModel(page, "cloud:gaia");
  await send(page, "And now from the cloud");
  const second = page.getByTestId("assistant-message").nth(1);
  await expect(second).toContainText("Cloud answer: And now from the cloud");
  await expect(second.getByTestId("message-model")).toHaveText("GLM-5.3 Flash · cloud");

  await pickModel(page, "local:qwen3:8b");
  await send(page, "Third");
  await expect(page.getByTestId("assistant-message").nth(2)).toContainText("Echo from qwen3:8b: Third");
  // The whole conversation was kept across the three models
  await page.reload();
  await expect(page.getByTestId("assistant-message")).toHaveCount(3);
});

test("the last selected model is remembered across sessions", async ({ page, context }) => {
  await open(page);
  await pickModel(page, "local:qwen3:8b");
  await expect(page.getByTestId("selected-model")).toHaveText("Qwen3 8B (Local)");
  const fresh = await context.newPage();
  await fresh.goto("/");
  await expect(fresh.getByTestId("selected-model")).toHaveText("Qwen3 8B (Local)");
});
