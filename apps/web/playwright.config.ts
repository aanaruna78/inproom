import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests against the real local stack (Keycloak, PostgreSQL, API, web).
 * Run with `make e2e` so .env is loaded. Uses the installed Google Chrome (no browser download).
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 0,
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  outputDir: "test-results",
  use: {
    baseURL: "http://localhost:3000",
    channel: "chrome",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      // Test-only fake GitHub (apps/api/tests/fakes/github.py) so E2E never touches real GitHub.
      command: "cd ../api && uv run python -m tests.fakes.github --port 18090",
      url: "http://localhost:18090/_fake/health",
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      // Test-only fake Jira (apps/api/tests/fakes/jira.py) so E2E never touches a real Jira site.
      command: "cd ../api && uv run python -m tests.fakes.jira --port 18091",
      url: "http://localhost:18091/_fake/health",
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      command: "cd ../api && uv run uvicorn app.main:app --port 8000",
      url: "http://localhost:8000/healthz",
      reuseExistingServer: true,
      timeout: 60_000,
      // The API may call the local fake providers (exact origins allowlisted for E2E only).
      env: {
        ...(process.env as Record<string, string>),
        INPROOM_GIT_ALLOWED_HOSTS: "api.github.com,http://localhost:18090",
        INPROOM_JIRA_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18091",
      },
    },
    {
      command: "npm run build && npm run start -- --port 3000",
      url: "http://localhost:3000",
      reuseExistingServer: true,
      timeout: 180_000,
    },
  ],
});
