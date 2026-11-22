/**
 * E2E: the meeting check during a call (live transcript) and afterwards: alerts with sources (conflict, to raise,
 * still pending, my action), unsourced ones dropped, the alert count on the recording indicator, Dismiss / Track
 * into My actions, the pop-up (count only, and only when switched on), and the alerts on the finished meeting.
 * Synthetic meetings and the fake local model only.
 */
import { expect, test, type APIRequestContext } from "@playwright/test";
import { ADMIN, API, open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

/** Notes for an earlier synthetic meeting (Release 4.2 go/no-go), written by the fake local model. */
async function earlierNotes(request: APIRequestContext) {
  const res = await request.post(`${API}/api/meetings/transcripts/fx-release-review/summary`,
    { data: { summary_model: "local:gemma4:e4b" } });
  expect(await res.text()).toContain('"type": "done"');
}

test("check a call while it's recorded, act on the alerts, and see them on the finished meeting", async ({ page, request }) => {
  await earlierNotes(request);
  await request.post(`${ADMIN}/config`, { data: { job_seconds: 2, live_line_seconds: 0.4 } });
  await request.put(`${API}/api/preferences`, { data: { check_popups: true } });
  // Record the pop-ups the page shows (and pretend Quorum is in the background, behind the meeting app).
  await page.addInitScript(() => {
    const shown: string[] = [];
    (window as unknown as { __popups: string[] }).__popups = shown;
    class FakeNotification {
      static permission = "granted";
      static requestPermission = async () => "granted";
      onclick: (() => void) | null = null;
      constructor(_title: string, options: { body: string }) { shown.push(options.body); }
      close() {}
    }
    (window as unknown as { Notification: unknown }).Notification = FakeNotification;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => (window as unknown as { __hidden?: boolean }).__hidden ?? false });
  });

  await open(page, "/meetings");
  await page.getByTestId("source-record").click();
  await page.getByLabel("Title").fill("Release sync");
  await page.getByRole("switch", { name: "Transcribe during the meeting" }).click();
  await page.getByTestId("record-start").click();
  const live = page.getByTestId("live-panel");
  await expect(live.getByTestId("live-lines")).toContainText("You: I'll tell the stakeholders.", { timeout: 10_000 });

  await live.getByTestId("live-tab-alerts").click();
  await page.evaluate(() => { (window as unknown as { __hidden: boolean }).__hidden = true; });
  await live.getByTestId("check-now").click();
  await expect(live.getByTestId("alert-conflict")).toContainText("The date moves to the 14th");
  await expect(live.getByTestId("alert-conflict").getByTestId("alert-source")).toContainText("Release 4.2 go/no-go");
  await expect(live.getByTestId("alert-raise")).toContainText("QA sign-off is still pending");
  await expect(live.getByTestId("alert-pending")).toContainText("Still open from an earlier meeting");
  await expect(live.getByTestId("alert-my_action")).toContainText("Update the release plan");
  await expect(live.getByText("The budget was cut.")).toHaveCount(0);  // no source: dropped
  await expect(page.getByTestId("indicator-alerts")).toHaveText("4");
  const popups = await page.evaluate(() => (window as unknown as { __popups: string[] }).__popups);
  expect(popups).toEqual(["4 alerts: 1 conflict, 1 to raise with everyone, 1 still pending, 1 action for you"]);

  // Checking again shows nothing new (no repeats).
  await live.getByTestId("check-now").click();
  await expect(live.getByTestId("check-status")).toContainText("Checked");
  await expect(live.locator("[data-testid^=alert-]").filter({ has: page.getByTestId("alert-dismiss") })).toHaveCount(4);

  await live.getByTestId("alert-raise").getByTestId("alert-dismiss").click();
  await expect(page.getByTestId("indicator-alerts")).toHaveText("3");
  await live.getByTestId("alert-pending").getByTestId("alert-track").click();
  await expect(live.getByTestId("alert-pending")).toHaveAttribute("data-state", "tracked");

  // Stop; My actions lists the tracked alert; the finished meeting shows the alerts found during the call.
  await page.getByTestId("record-stop").click();
  await expect(page.getByTestId("job-panel")).toHaveAttribute("data-status", "done", { timeout: 15_000 });
  await expect(page.getByTestId("my-actions").getByTestId("my-action")).toContainText("Still open from an earlier meeting");
  await page.getByTestId("my-actions").getByRole("checkbox").click();
  await expect(page.getByTestId("my-action")).toHaveAttribute("data-done", "true");
  const list = await (await request.get(`${API}/api/meetings/transcripts`)).json();
  const call = list.find((t: { title: string }) => t.title.startsWith("Release sync"));
  await open(page, `/meetings/${call.id}`);
  await page.getByTestId("tab-check").click();
  await expect(page.getByTestId("meeting-check").getByTestId("alert-conflict")).toBeVisible();
  await expect(page.getByTestId("meeting-check").getByTestId("alert-raise")).toHaveCount(0);  // dismissed stays dismissed
});

test("with pop-ups off, alerts only show in the app", async ({ page, request }) => {
  await earlierNotes(request);
  await request.post(`${ADMIN}/config`, { data: { live_line_seconds: 0.4 } });
  await page.addInitScript(() => {
    const shown: string[] = [];
    (window as unknown as { __popups: string[] }).__popups = shown;
    class FakeNotification {
      static permission = "granted";
      static requestPermission = async () => "granted";
      constructor(_title: string, options: { body: string }) { shown.push(options.body); }
      close() {}
    }
    (window as unknown as { Notification: unknown }).Notification = FakeNotification;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
  });
  await request.put(`${API}/api/preferences`, { data: { check_popups: false } });
  await open(page, "/meetings");
  await page.getByTestId("source-record").click();
  await page.getByRole("switch", { name: "Transcribe during the meeting" }).click();
  await page.getByTestId("record-start").click();
  const live = page.getByTestId("live-panel");
  await expect(live.getByTestId("live-lines")).toContainText("You:", { timeout: 10_000 });
  await live.getByTestId("live-tab-alerts").click();
  await live.getByTestId("check-now").click();
  await expect(live.getByTestId("alert-conflict")).toBeVisible();
  await expect(page.getByTestId("indicator-alerts")).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __popups: string[] }).__popups)).toEqual([]);
  await page.getByTestId("record-stop").click();
});
