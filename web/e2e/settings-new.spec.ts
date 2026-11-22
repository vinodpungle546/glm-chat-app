/** E2E: Settings → Import, Search and Skills tabs (meeting-notes skill and template editing). */
import { expect, test } from "@playwright/test";
import { chooseSummaryModel, open, openSettingsTab, resetAll } from "./helpers";

test.beforeEach(async ({ request }) => resetAll(request));

test("Import from Claude tab explains both export routes", async ({ page }) => {
  await open(page);
  await openSettingsTab(page, "import");
  const steps = page.getByTestId("import-instructions");
  await expect(steps).toContainText("Settings → Privacy → Export data");
  await expect(steps).toContainText("export memory");
  await expect(page.getByTestId("import-preview-button")).toBeDisabled();
});

test("Search tab: provider, key (masked, never shown), test, results and sources", async ({ page }) => {
  await open(page);
  await openSettingsTab(page, "search");
  await expect(page.getByTestId("provider-tavily")).toHaveAttribute("aria-checked", "true");
  await page.getByRole("switch", { name: "Include web results" }).click();
  await expect(page.getByRole("switch", { name: "Include web results" })).toHaveAttribute("aria-checked", "true");
  await page.getByTestId("search-key-input").fill("tvly-test-key-1234");
  await page.getByTestId("save-search-key").click();
  await expect(page.getByTestId("provider-tavily")).toContainText("Key saved");
  await expect(page.getByTestId("search-key-input")).toHaveAttribute("placeholder", /••••1234/);
  await expect(page.getByTestId("search-key-input")).toHaveValue("");
  await page.getByTestId("test-search").click();
  await expect(page.getByText(/Connected · \d+ result/)).toBeVisible();
  await page.getByTestId("provider-exa").click();
  await expect(page.getByTestId("provider-exa")).toHaveAttribute("aria-checked", "true");
  await expect(page.getByTestId("provider-exa")).toContainText("Needs key");
  await page.getByTestId("max-results").fill("8");
  await page.getByRole("switch", { name: "Connectors" }).click();
  await page.keyboard.press("Escape");
  await openSettingsTab(page, "search");
  await expect(page.getByTestId("max-results")).toHaveValue("8");
  await expect(page.getByRole("switch", { name: "Connectors" })).toHaveAttribute("aria-checked", "true");
  await expect(page.getByTestId("index-status")).toContainText("nomic-embed-text");
});

test("Skills tab: the meeting notes skill — view, disable/enable, edit and reset a template", async ({ page }) => {
  await open(page);
  await openSettingsTab(page, "skills");
  const skill = page.getByTestId("builtin-skill");
  await expect(skill).toContainText("Professional Meeting Notes");
  await expect(skill).toContainText("Default for meeting summaries");
  await skill.getByTestId("view-skill").click();
  await expect(page.getByTestId("skill-instructions")).toContainText("Only include what was actually said");
  await page.keyboard.press("Escape");

  await skill.getByLabel("Edit template Daily stand-up").click();
  const text = page.getByTestId("template-text");
  await expect(text).toHaveValue(/Stand-up updates/);
  await text.fill((await text.inputValue()) + "\n_Custom stand-up footer_\n");
  await page.getByTestId("save-template").click();
  await expect(skill.getByTestId("template-row").filter({ hasText: "Daily stand-up" })).toContainText("Customised");

  await skill.getByRole("switch", { name: "Use for meeting summaries" }).click();
  await expect(skill).toContainText("Off");
  await page.keyboard.press("Escape");
  await page.goto("/meetings/fx-standup");
  await page.getByTestId("tab-summary").click();
  await expect(page.getByTestId("generate-summary")).toContainText("Generate summary");

  await openSettingsTab(page, "skills");
  await page.getByTestId("builtin-skill").getByRole("switch", { name: "Use for meeting summaries" }).click();
  await page.keyboard.press("Escape");
  await page.getByTestId("generate-summary").click();
  await chooseSummaryModel(page);
  await expect(page.getByTestId("notes")).toContainText("Custom stand-up footer");

  await openSettingsTab(page, "skills");
  await page.getByTestId("builtin-skill").getByLabel("Edit template Daily stand-up").click();
  await page.getByTestId("reset-template").click();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("builtin-skill").getByTestId("template-row").filter({ hasText: "Daily stand-up" })).not.toContainText("Customised");
});
