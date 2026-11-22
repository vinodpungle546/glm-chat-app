/**
 * E2E: theme, keyboard shortcuts, tablet layout, and clear errors when a model, Ollama, the cloud or the
 * API fails.
 */
import { expect, test } from "@playwright/test";
import { ADMIN, API, open, pickModel, resetAll, send, setOllama } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

const mod = process.platform === "darwin" ? "Meta" : "Control";

test("theme toggle follows the system by default and switches light/dark", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await open(page);
  await expect(page.locator("html")).toHaveClass(/dark/);
  const toggle = page.getByTestId("theme-toggle");
  await expect(toggle).toHaveAttribute("data-theme-mode", "system");
  await toggle.click();
  await expect(toggle).toHaveAttribute("data-theme-mode", "light");
  await expect(page.locator("html")).not.toHaveClass(/dark/);
  await toggle.click();
  await expect(page.locator("html")).toHaveClass(/dark/);
});

test("keyboard shortcuts", async ({ page }) => {
  await open(page);
  await page.keyboard.press(`${mod}+B`);
  await expect(page.getByTestId("sidebar")).toHaveAttribute("data-open", "false");
  await page.keyboard.press(`${mod}+B`);
  await expect(page.getByTestId("sidebar")).toHaveAttribute("data-open", "true");
  await page.keyboard.press(`${mod}+K`);
  await expect(page.getByTestId("search-dialog")).toBeVisible();
  await expect(page.getByTestId("search-input")).toBeFocused();
  await page.keyboard.press("Escape");
  await page.keyboard.press(`${mod}+Shift+F`);
  await expect(page.getByLabel("Search chats")).toBeFocused();
  await page.keyboard.press(`${mod}+Shift+M`);
  await expect(page).toHaveURL(/\/meetings$/);
  await page.keyboard.press(`${mod}+Shift+O`);
  await expect(page).toHaveURL(/\/$/);
  await page.keyboard.press(`${mod}+,`);
  await expect(page.getByTestId("settings")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press(`${mod}+/`);
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
});

test("responsive: on a tablet the sidebar is an overlay drawer", async ({ page }) => {
  await page.setViewportSize({ width: 820, height: 1100 });
  await open(page);
  const sidebar = page.getByTestId("sidebar");
  await expect(sidebar).toHaveAttribute("data-open", "false");
  await page.getByTestId("open-sidebar").click();
  await expect(sidebar).toBeInViewport();
  await page.mouse.click(800, 500); // the dimmed backdrop closes it
  await expect(sidebar).toHaveAttribute("data-open", "false");
  await page.getByTestId("open-sidebar").click();
  await page.keyboard.press("Escape");
  await expect(sidebar).toHaveAttribute("data-open", "false");
  await page.getByTestId("open-sidebar").click();
  await page.keyboard.press(`${mod}+Shift+M`); // navigating closes the drawer
  await expect(page).toHaveURL(/\/meetings$/);
  await expect(sidebar).toHaveAttribute("data-open", "false");
  await page.goto("/meetings/t-standup");
  await expect(page.getByRole("tab", { name: "Transcript" })).toBeVisible(); // stacked tabs instead of two columns
});

test("model unavailable: not downloaded", async ({ page, request }) => {
  await setOllama(request, { installed: ["qwen3:8b"] });
  await open(page);
  await expect(page.getByText("Gemma 4 E4B isn't downloaded")).toBeVisible();
  await expect(page.getByTestId("send")).toBeDisabled();
  await page.getByRole("button", { name: "Download model" }).click();
  await expect(page.getByTestId("local-model-gemma4:e4b")).toBeVisible();
});

test("Ollama stops while chatting: clear error with fix-it steps and a model switch", async ({ page, request }) => {
  await open(page);
  await send(page, "First question");
  await expect(page.getByTestId("assistant-message")).toContainText("Echo from gemma4:e4b");
  await setOllama(request, { running: false });
  await send(page, "Second question");
  const error = page.getByTestId("chat-error");
  await expect(error).toContainText("not reachable");
  await expect(error).toContainText("ollama serve");
  await error.getByRole("button", { name: "Switch to a cloud model" }).click();
  await expect(page.getByTestId("selected-model")).toHaveText("GLM-5.3 Flash");
  await setOllama(request, { running: true });
});

test("cloud API failure shows an error with retry", async ({ page, request }) => {
  await request.post(`${ADMIN}/config`, { data: { gaia_fail: true } });
  await open(page);
  await pickModel(page, "cloud:gaia");
  await send(page, "Hello cloud");
  await expect(page.getByTestId("chat-error")).toContainText("Request failed");
  await request.post(`${ADMIN}/config`, { data: { gaia_fail: false } });
  await page.getByTestId("retry").click();
  await expect(page.getByTestId("assistant-message")).toContainText("Cloud answer: Hello cloud");
});

test("network failure to the API is reported", async ({ page }) => {
  await open(page);
  await page.route(`${API.replace("127.0.0.1", "localhost")}/api/sessions/*/messages`, (route) => route.abort("connectionrefused"));
  await send(page, "Will this reach the API?");
  await expect(page.getByTestId("chat-error")).toContainText("Can't reach the Quorum API");
});

test("confidential text with a cloud model asks first and can stay local", async ({ page }) => {
  await open(page);
  await pickModel(page, "cloud:gaia");
  await send(page, "Here are the confidential salary numbers for review");
  await expect(page.getByText("This looks confidential")).toBeVisible();
  await page.getByTestId("use-local").click();
  await expect(page.getByTestId("assistant-message")).toContainText("Echo from gemma4:e4b");
  await expect(page.getByText("Confidential chat — answered by a local model")).toBeVisible();
});
