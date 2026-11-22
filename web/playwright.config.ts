/**
 * Playwright (end-to-end test) configuration. Starts the fake services (mock_services.py), the real API on
 * :18000 pointed at them, and a Next.js dev server on :13000. One worker, because all tests share the same
 * backend state.
 */
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// End-to-end tests run the real web app + real Python API against fake external services
// (Ollama, GAIA cloud, transcriber — see e2e/mock_services.py) and a throwaway data folder.
const PY = "../.venv/bin/python";
const DATA = "web/e2e/.data";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:13000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1360, height: 860 } } }],
  webServer: [
    {
      command: `rm -rf e2e/.data && mkdir -p e2e/.data && ${PY} e2e/mock_services.py admin --port 18999 --ollama-port 18434 --state e2e/.data/mock-state.json`,
      url: "http://127.0.0.1:18999/__admin/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: `cd .. && mkdir -p ${DATA}/api && exec .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 18000`,
      url: "http://127.0.0.1:18000/api/health",
      reuseExistingServer: false,
      timeout: 30_000,
      env: {
        APP_DATA_DIR: `${DATA}/api`,
        PROJECTS_DIR: `${DATA}/api/projects`,
        SESSIONS_DIR: `${DATA}/api/sessions`,
        CONNECTORS_FILE: `${DATA}/api/connectors.json`,
        PEOPLE_FILE: `${DATA}/api/people.json`,
        LOCAL_LLM_BASE_URL: "http://127.0.0.1:18434",
        TRANSCRIBER_URL: "http://127.0.0.1:18999/transcriber",
        GAIA_ENDPOINT: "http://127.0.0.1:18999/gaia/v1/chat/completions",
        GAIA_API_KEY: "test-key",
        OLLAMA_CONTEXT_LENGTH: "16384",
        WEB_PUBLIC_URL: "http://localhost:13000",
        SECRETS_BACKEND: "file",
        TAVILY_URL: "http://127.0.0.1:18999/tavily/search",
        EXA_URL: "http://127.0.0.1:18999/exa/search",
        BRAVE_URL: "http://127.0.0.1:18998/res/v1/web/search", // nothing listens: "offline"
        WEB_ORIGINS: "http://localhost:13000,http://127.0.0.1:13000",
        ANTHROPIC_PROVIDER_URL: "http://127.0.0.1:18997",          // fake Claude API (tests/fake_cloud.py)
        OPENAI_PROVIDER_URL: "http://127.0.0.1:18997/openai/v1",   // fake OpenAI API
        MCP_REGISTRY_URL: "http://127.0.0.1:18996",                // fake MCP Registry (tests/fake_mcp.py)
        API_PUBLIC_URL: "http://127.0.0.1:18000",
        // local packages never install: the fake stdio server runs instead of npx/uvx
        CONNECTORS_TEST_STDIO: `${JSON.stringify(path.resolve(__dirname, "../.venv/bin/python"))} ${JSON.stringify(path.resolve(__dirname, "../tests/fake_mcp_stdio.py"))}`,
      },
    },
    {
      command: "npx next dev -p 13000",
      url: "http://localhost:13000",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { NEXT_DIST_DIR: ".next-e2e", NEXT_PUBLIC_API_URL: "http://localhost:18000", NEXT_TELEMETRY_DISABLED: "1" },
    },
  ],
});
