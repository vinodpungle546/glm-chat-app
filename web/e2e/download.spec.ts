/** E2E: downloading a local model from the model picker, with progress and cancel. */
import { expect, test } from "@playwright/test";
import { open, resetAll, setOllama } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("download a model: start, progress, cancel, then complete", async ({ page, request }) => {
  await setOllama(request, { installed: ["qwen3:8b"], pull_steps: 40, pull_step_seconds: 0.15 });
  await open(page);
  await page.getByTestId("open-settings").click();
  await page.getByTestId("settings-tab-models").click();
  const row = page.getByTestId("local-model-gemma4:e4b");
  await expect(row.getByTestId("status-local:gemma4:e4b")).toHaveText("Not downloaded");
  await expect(row).toContainText("about 9.6 GB");

  // Start, watch progress, cancel
  await row.getByTestId("download-gemma4:e4b").click();
  await expect(row.getByRole("progressbar")).toBeVisible();
  await expect(row).toContainText(/\d+%/);
  await row.getByTestId("cancel-gemma4:e4b").click();
  await expect(row).toContainText("Download cancelled");
  await expect(row.getByTestId("download-gemma4:e4b")).toBeVisible();

  // Start again and let it finish
  await setOllama(request, { pull_steps: 6, pull_step_seconds: 0.1 });
  await row.getByTestId("download-gemma4:e4b").click();
  await expect(row.getByTestId("status-local:gemma4:e4b")).toHaveText("Ready", { timeout: 15_000 });
  await expect(page.getByText("gemma4:e4b downloaded")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId("model-picker").click();
  await expect(page.getByTestId("status-local:gemma4:e4b")).toHaveText("Ready");
});
