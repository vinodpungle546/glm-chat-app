/**
 * E2E: every Settings tab opens and works (general, projects, memory, skills, connectors, local models).
 */
import { expect, test } from "@playwright/test";
import { open, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("all settings tabs: general, projects, memory, skills, connectors, local models", async ({ page }) => {
  await open(page);
  await page.getByTestId("open-settings").click();
  const dialog = page.getByTestId("settings");
  await expect(dialog.getByText("Ask before sending confidential content to the cloud")).toBeVisible();

  // Projects: create and switch
  await page.getByTestId("settings-tab-projects").click();
  await dialog.getByLabel("New project name").fill("Payments team");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(dialog.getByTestId("project-list")).toContainText("Payments team");
  await expect(dialog.getByText("Payments team settings")).toBeVisible();

  // Memory: a fact and a person
  await page.getByTestId("settings-tab-memory").click();
  await expect(dialog.getByText("This project · Payments team")).toBeVisible();
  await dialog.getByLabel("New fact").fill("Sprint reviews are on Fridays");
  await dialog.getByLabel("New fact").press("Enter");
  await expect(dialog.getByTestId("memory-list")).toContainText("Sprint reviews are on Fridays");
  await dialog.getByLabel("Person name").fill("Priya");
  await dialog.getByLabel("Person role").fill("Delivery manager");
  await dialog.getByRole("button", { name: "Add person" }).click();
  await expect(dialog.getByTestId("person-row")).toContainText("Delivery manager");

  // Skills
  await page.getByTestId("settings-tab-skills").click();
  await expect(dialog.getByText("Skills · Payments team")).toBeVisible();
  await dialog.getByRole("button", { name: "New skill" }).click();
  await dialog.getByLabel("Name").fill("Action items");
  await dialog.getByLabel("Instructions").fill("List action items as a table.");
  await dialog.getByRole("button", { name: "Save skill" }).click();
  await expect(dialog.getByTestId("skill-row")).toContainText("/Action items");

  // Connectors
  await page.getByTestId("settings-tab-connectors").click();
  await dialog.getByLabel("Name").fill("Files");
  await dialog.getByLabel("URL").fill("http://localhost:9000/mcp");
  await dialog.getByRole("button", { name: "Add connector" }).click();
  await expect(dialog.getByTestId("connector-list")).toContainText("On this Mac");
  await dialog.getByRole("button", { name: "Remove Files" }).click();
  await page.getByRole("button", { name: "Remove", exact: true }).click();  // confirm: stored keys are deleted too
  await expect(dialog.getByText("No connectors")).toBeVisible();

  // Local models
  await page.getByTestId("settings-tab-models").click();
  await expect(dialog.getByTestId("local-model-gemma4:e4b")).toContainText("Gemma 4 E4B");
  await expect(dialog.getByText("Ollama 0.40.16")).toBeVisible();

  // The skill is offered in the composer of the new project
  await page.keyboard.press("Escape");
  await page.getByTestId("composer-input").fill("/");
  await expect(page.getByRole("listbox", { name: "Skills" })).toContainText("/Action items");
});
