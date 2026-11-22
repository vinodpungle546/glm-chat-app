/**
 * E2E: speaker labels use imported memory and past conversations and say why; without the import the same
 * speaker stays unnamed.
 */
import { expect, test } from "@playwright/test";
import { importExport, open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("speaker labels use imported memory and past conversations, and say why", async ({ page, request }) => {
  await importExport(request);
  await open(page, "/meetings/t-standup");
  await page.getByTestId("identify-speakers").click();
  const dev = page.locator('[data-speaker="SPEAKER_02"]');
  await expect(dev.getByTestId("speaker-name")).toHaveText("Mai Tran");
  await expect(dev.getByTestId("speaker-why")).toHaveText("Matched to Mai Tran: backend developer in claude.ai memory");
  await expect(dev).toContainText("backend developer");
  await dev.getByRole("button", { name: "Why" }).click();
  await expect(dev).toContainText("appears in 1 past conversation about these topics (“Refund endpoint rollout”)");
  await expect(dev.getByTestId("confidence")).toHaveText(/Low · \d+%/);
});

test("without the import, the same speaker stays unnamed", async ({ page }) => {
  await open(page, "/meetings/t-standup");
  await page.getByTestId("identify-speakers").click();
  await expect(page.locator('[data-speaker="SPEAKER_01"]').getByTestId("speaker-name")).toHaveText("Owen");
  await expect(page.locator('[data-speaker="SPEAKER_02"]').getByTestId("speaker-name")).toHaveText("Speaker 3");
});
