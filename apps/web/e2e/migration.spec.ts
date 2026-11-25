import { spawn, type ChildProcess } from "node:child_process";

import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-154 / story INPROOM-19: onboard an existing Jira project (fake Jira LEG, 42 issues) into a MIGRATION
 * project: plan -> scan (background worker) -> preview tree + diagnostics -> confirm baseline -> project ACTIVE;
 * a later Jira change shows up as drift; viewers are read-only.
 */

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const API = "http://localhost:8000";
const FAKE = "http://localhost:18091";
const TOKEN_URL = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";
const EMAIL = "bot@acme.example";
const VALID_TOKEN = "ATATT3xFfGF0VALID00000000000000000000000000000000000000000000000000000000=AB12CD34";

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

async function auth(request: APIRequestContext, username: string) {
  const r = await request.post(TOKEN_URL, {
    form: { grant_type: "password", client_id: "inproom-dev-cli", username, password: `${username}-local` },
  });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

async function signedInPage(browser: Browser, path: string, username: string) {
  const page = await (await browser.newContext()).newPage();
  await page.goto(path);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(`${username}-local`);
  await page.locator("#kc-login").click();
  await expect(page.getByTestId("signed-in-user")).toHaveText(`${username}@tenant-a.local`);
  return page;
}

async function eventually(page: Page, check: () => Promise<void>, timeout = 45_000) {
  await expect(async () => {
    await page.reload();
    await check();
  }).toPass({ timeout });
}

let worker: ChildProcess | undefined;
test.afterAll(() => {
  worker?.kill("SIGTERM");
});

test("onboard an existing Jira project: plan, scan, preview, confirm baseline, drift", async ({ browser, request }) => {
  test.setTimeout(180_000);
  expect((await request.post(`${FAKE}/_fake/reset`)).status()).toBe(204);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `mig-${run}`, name: `Migration ${run}` } })).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `MG${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Legacy product ${run}`, mode: "MIGRATION" },
    })
  ).json();
  expect(project.status).toBe("PENDING_SOURCES");
  for (const [user, role] of [
    ["dave", "project_admin"],
    ["erin", "viewer"],
  ] as const) {
    const me = await (await request.get(`${API}/v1/me`, { headers: await auth(request, user) })).json();
    await request.put(`${API}/v1/projects/${project.id}/members/${me.user_id}`, { headers: alice, data: { roles: [role] } });
  }
  const connection = await (
    await request.post(`${API}/v1/connections`, {
      headers: alice,
      data: { provider: "jira", display_name: `Jira ${run}`, base_url: FAKE, email: EMAIL, token: VALID_TOKEN },
    })
  ).json();
  expect((await request.put(`${API}/v1/projects/${project.id}/sources/jira/${connection.id}:LEG`, { headers: alice })).status()).toBe(200);
  const tenantId = (await (await request.get(`${API}/v1/me`, { headers: alice })).json()).tenant_id;
  worker = spawn("uv", ["run", "python", "-m", "app.worker", "--poll", "1", "--tick", "1", "--tenant", tenantId], {
    cwd: "../api",
    env: {
      ...process.env,
      INPROOM_SYNC_INTERVAL_SECONDS: "600",
      INPROOM_HEALTH_INTERVAL_SECONDS: "600",
      INPROOM_JIRA_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18091",
      INPROOM_CONFLUENCE_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18092",
      INPROOM_GIT_ALLOWED_HOSTS: "api.github.com,http://localhost:18090",
    },
    stdio: "ignore",
  });

  // 1) dave opens the migration wizard from the project page and saves the plan.
  const dave = await signedInPage(browser, `/projects/${project.id}`, "dave");
  await dave.getByTestId("migration-link").click();
  await expect(dave.getByTestId("write-readiness")).toContainText("no baseline confirmed yet");
  await dave.getByTestId("migration-plan").locator('select[name="jira_source_id"]').selectOption({ label: "LEG · LEG legacy" });
  await dave.getByTestId("migration-plan").getByRole("button", { name: "Save plan" }).click();
  await expect(dave.getByTestId("plan-summary")).toContainText("LEG");
  await shot(dave, "migration-01-plan");

  // 2) Scan: the background worker inventories every issue.
  await dave.getByTestId("migration-scan").getByRole("button", { name: "Scan" }).click();
  await eventually(dave, async () => expect(dave.getByTestId("scan-status")).toContainText("SUCCEEDED", { timeout: 1_000 }));
  await expect(dave.getByTestId("inventory-counts")).toContainText("42 issues");

  // 3) Preview: the Jira tree with original keys, and diagnostics explained, not repaired.
  const epic = dave.locator('[data-testid="inventory-row"][aria-label="LEG-1"]');
  await expect(epic).toContainText("epic");
  await expect(dave.getByTestId("diagnostics")).toContainText("orphan");
  await expect(dave.getByTestId("diagnostics")).toContainText("OLD-1");
  await expect(dave.getByTestId("diagnostics")).toContainText("OTHER-7");
  await expect(dave.getByTestId("diagnostics")).toContainText("Spike");
  await shot(dave, "migration-02-preview");
  await epic.getByRole("link", { name: "4 →" }).click();
  await expect(dave.locator('[data-testid="inventory-row"][aria-label="LEG-2"]')).toContainText("Story 0.0");
  await shot(dave, "migration-03-tree-drilldown");

  // 4) Confirm the baseline: the project becomes ACTIVE.
  await dave.getByTestId("migration-confirm").getByRole("button", { name: "Confirm baseline" }).click();
  await expect(dave.getByTestId("baseline")).toContainText("42");
  await expect(dave.getByRole("heading", { level: 1 })).toContainText("ACTIVE");
  await expect(dave.getByTestId("write-readiness")).toContainText("Baseline confirmed and current");
  await shot(dave, "migration-04-baselined");

  // 5) A change in Jira after the baseline shows up as drift; the baseline stays.
  await request.post(`${FAKE}/_fake/edit-issue`, { data: { key: "LEG-2", status: "Done" } });
  await dave.getByTestId("migration-scan").getByRole("button", { name: "Re-sync" }).click();
  await eventually(dave, async () => expect(dave.getByTestId("drift")).toContainText("1 changed", { timeout: 1_000 }));
  await expect(dave.getByTestId("drift")).toContainText("LEG-2: status");
  await expect(dave.getByTestId("baseline")).toContainText("42");
  await shot(dave, "migration-05-drift");

  // 6) erin (viewer) sees the migration read-only.
  const erin = await signedInPage(browser, `/projects/${project.id}/migration`, "erin");
  await expect(erin.getByTestId("baseline")).toBeVisible();
  await expect(erin.getByTestId("migration-plan")).toHaveCount(0);
  await expect(erin.getByTestId("migration-scan")).toHaveCount(0);
  await request.post(`${FAKE}/_fake/reset`);
});
