/**
 * Shared helpers for the end-to-end tests: reset the fake services and app data, open pages, pick models,
 * send messages.
 */
import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const API = "http://127.0.0.1:18000";
export const ADMIN = "http://127.0.0.1:18999/__admin";

/** Fresh state for every test: fake services reset, no chats, default model, no people. */
/**
 * Runs before each test: deletes summaries, labels, model choices, chats, people, memory, providers and
 * connectors through the API, resets the fakes and puts preferences back to their defaults.
 */
export async function resetAll(request: APIRequestContext) {
  // Clear saved notes and labels first; the mock reset then restores the transcripts (with their names).
  for (const t of await (await request.get(`${API}/api/meetings/transcripts`)).json()) {
    await request.delete(`${API}/api/meetings/transcripts/${t.id}/summary`);
    await request.delete(`${API}/api/meetings/transcripts/${t.id}/speakers`);
    await request.delete(`${API}/api/meetings/transcripts/${t.id}/models`);
    await request.delete(`${API}/api/meetings/check/alerts?transcript_id=${t.id}`);
  }
  for (const a of await (await request.get(`${API}/api/my-actions`)).json()) await request.delete(`${API}/api/my-actions/${a.id}`);
  await request.post(`${ADMIN}/reset`);
  await request.delete(`${API}/api/imports/claude`);
  for (const s of await (await request.get(`${API}/api/sessions`)).json()) await request.delete(`${API}/api/sessions/${s.id}`);
  for (const p of await (await request.get(`${API}/api/people`)).json()) await request.delete(`${API}/api/people/${p.id}`);
  for (const e of (await (await request.get(`${API}/api/memory`)).json()).entries) await request.delete(`${API}/api/memory/entries/${e.id}`);
  for (const p of (await (await request.get(`${API}/api/providers`)).json()).providers) await request.delete(`${API}/api/providers/${p.id}`);
  await request.put(`${API}/api/preferences`, { data: { selected_model: "local:gemma4:e4b", active_project: "default",
                                                        confirm_cloud_sensitive: true, web_search: false, meeting_notes_skill: true,
                                                        imported_cloud_notice: false, cloud_notices: [], voiceprints: true,
                                                        speaker_cloud_review: true, screen_names: true, vision_fallback: true,
                                                        vision_cap: 12, screen_debug: false, vision_model: "", speaker_model: "", summary_model: "",
                                                        record_live: false, check_model: "", check_popups: true,
                                                        check_auto_minutes: 0, check_days: 90, check_all_projects: false,
                                                        search: { web_enabled: false, provider: "tavily", max_results: 5,
                                                                  sources: { chats: true, transcripts: true, memory: true, connectors: false } } } });
  for (const provider of ["tavily", "brave", "exa"]) await request.delete(`${API}/api/search/keys/${provider}`);
  const skill = (await (await request.get(`${API}/api/skills/builtin`)).json())[0];
  for (const t of skill.templates) if (t.customized) await request.delete(`${API}/api/skills/meeting-notes/templates/${t.name}`);
  const project = await (await request.get(`${API}/api/projects/default`)).json();
  await request.patch(`${API}/api/projects/${project.id}`, { data: { memory: [], skills: [], name: "General" } });
  for (const c of await (await request.get(`${API}/api/connectors`)).json()) await request.delete(`${API}/api/connectors/${c.id}`);
}

/** Changes the fake Ollama (running or stopped, installed models, speed…). */
export async function setOllama(request: APIRequestContext, state: Record<string, unknown>) {
  await request.post(`${ADMIN}/ollama`, { data: state });
}

/** Opens a page with empty browser storage and waits until the model list has loaded. */
export async function open(page: Page, path = "/") {
  await page.addInitScript(() => window.localStorage.clear());
  await page.goto(path);
  await expect(page.getByTestId("selected-model")).not.toHaveText(/Loading/);
}

/** Selects a chat model in the sidebar picker. */
export async function pickModel(page: Page, id: string) {
  await page.getByTestId("model-picker").click();
  await page.getByTestId(`model-option-${id}`).click();
}

/** Types a chat message and sends it. */
export async function send(page: Page, text: string) {
  await page.getByTestId("composer-input").fill(text);
  await page.getByTestId("send").click();
}

export const FIXTURES = `${__dirname}/fixtures`;

/** Import a synthetic claude.ai export through the API (all items). */
/** Imports a synthetic claude.ai export fixture through the API (preview, then commit everything). */
export async function importExport(request: APIRequestContext, file = "claude-export.zip") {
  const fs = await import("node:fs");
  const preview = await (await request.post(`${API}/api/imports/claude/preview`, {
    multipart: { files: { name: file, mimeType: "application/octet-stream", buffer: fs.readFileSync(`${FIXTURES}/${file}`) } },
  })).json();
  return (await request.post(`${API}/api/imports/claude/commit`, { data: { preview_id: preview.preview_id } })).json();
}

/** Opens Settings on the given tab. */
export async function openSettingsTab(page: Page, tab: string) {
  await page.getByTestId("open-settings").click();
  await page.getByTestId(`settings-tab-${tab}`).click();
}

/** Add a cloud provider through the API (the fake services accept these keys) and enable models. */
/** Adds a cloud provider with the fake's test key and switches on the given models. */
export async function addProvider(request: APIRequestContext, kind: "anthropic" | "openai", enabled: string[]) {
  const key = kind === "anthropic" ? "sk-ant-test" : "sk-openai-test";
  const added = await (await request.post(`${API}/api/providers`, { data: { kind, key } })).json();
  await request.post(`${API}/api/providers/${added.id}/test`);
  await request.patch(`${API}/api/providers/${added.id}`, { data: { enabled } });
  return added;
}


/** Summaries don't follow the chat model: when no model is chosen, Generate asks which one writes
 *  the summary. Pick it in that dialog (it's then kept for the meeting). */
export async function chooseSummaryModel(page: Page, modelId = "local:gemma4:e4b") {
  const dialog = page.getByTestId("summary-model-dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Model", { exact: true }).selectOption(modelId);
  await page.getByTestId("summary-model-ok").click();
}
