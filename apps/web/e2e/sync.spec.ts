import { spawn, type ChildProcess } from "node:child_process";

import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-144 / story INPROOM-25: a change made in Confluence with NO webhook is picked up by the background
 * worker's schedule; Sync status shows freshness; "Reconcile now" coalesces; a webhook secret is shown once;
 * viewers are read-only. The worker runs with a 3-second interval for the test.
 */

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const API = "http://localhost:8000";
const FAKE = "http://localhost:18092";
const TOKEN_URL = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";
const EMAIL = "bot@acme.example";
const VALID_TOKEN = "ATATT3xFfGF0CONFLUENCE0000000000000000000000000000000000000000000000000=CF12AB34";

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

let worker: ChildProcess | undefined;

test.afterAll(() => {
  worker?.kill("SIGTERM");
});

test("missed webhook recovered by the scheduled worker; sync status; reconcile now; secret once", async ({ browser, request }) => {
  test.setTimeout(150_000);
  expect((await request.post(`${FAKE}/_fake/reset`)).status()).toBe(204);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (
    await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `sync-${run}`, name: `Sync ${run}` } })
  ).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `SY${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Sync project ${run}` },
    })
  ).json();
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
      data: { provider: "confluence", display_name: `Confluence ${run}`, base_url: FAKE, email: EMAIL, token: VALID_TOKEN },
    })
  ).json();
  const selected = await request.put(`${API}/v1/projects/${project.id}/sources/confluence/${connection.id}:ENG`, {
    headers: alice,
    data: { required_labels: ["approved"] },
  });
  expect(selected.status()).toBe(200);

  // The background worker (as `make worker` runs it), with a short schedule for the test. --tenant keeps it to
  // this tenant's jobs: the shared dev database also holds leftovers from other test runs.
  const tenantId = (await (await request.get(`${API}/v1/me`, { headers: alice })).json()).tenant_id;
  worker = spawn("uv", ["run", "python", "-m", "app.worker", "--poll", "1", "--tick", "1", "--tenant", tenantId], {
    cwd: "../api",
    env: {
      ...process.env,
      INPROOM_SYNC_INTERVAL_SECONDS: "3",
      INPROOM_GIT_ALLOWED_HOSTS: "api.github.com,http://localhost:18090",
      INPROOM_JIRA_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18091",
      INPROOM_CONFLUENCE_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18092",
    },
    stdio: "ignore",
  });

  // 1) dave: the first scheduled run makes the space fresh.
  const dave = await signedInPage(browser, `/projects/${project.id}/sources`, "dave");
  const row = dave.locator('[data-testid="sync-row"][aria-label="confluence ENG"]');
  await expect(async () => {
    await dave.reload();
    await expect(row.getByTestId("sync-health")).toHaveText("fresh", { timeout: 1_000 });
  }).toPass({ timeout: 45_000 });
  await shot(dave, "sync-01-fresh");

  // 2) A page is edited in Confluence and no webhook ever arrives: the schedule recovers it.
  await request.post(`${FAKE}/_fake/edit`, { data: { page_id: 101, body: "<p>edited without any webhook</p>" } });
  const catalog = `/projects/${project.id}/sources/confluence/${selected.ok() ? (await selected.json()).source.id : ""}`;
  await expect(async () => {
    await dave.goto(catalog);
    await expect(dave.locator('[data-testid="page-row"][aria-label="101"]')).toContainText("v4", { timeout: 1_000 });
  }).toPass({ timeout: 45_000 });
  await shot(dave, "sync-02-recovered-without-webhook");

  // 3) Reconcile now: one job however many clicks.
  await dave.goto(`/projects/${project.id}/sources`);
  worker.kill("SIGTERM"); // pause the worker so the queued job stays visible
  await new Promise((r) => setTimeout(r, 1_500));
  await row.getByTestId("reconcile").getByRole("button", { name: "Reconcile now" }).click();
  await expect(dave.getByTestId("reconcile-success")).toContainText("Reconcile queued.");
  await dave.locator('[data-testid="sync-row"][aria-label="confluence ENG"]').getByTestId("reconcile").getByRole("button", { name: "Reconcile now" }).click();
  await expect(dave.getByTestId("reconcile-success")).toContainText("Already queued (2 requests share one job).");
  await expect(row).toContainText("queued");
  await shot(dave, "sync-03-reconcile-coalesced");

  // 4) alice generates a Confluence webhook secret: shown once, gone after reload.
  const admin = await signedInPage(browser, "/integrations", "alice");
  const card = admin.getByTestId("connection").filter({ hasText: `Confluence ${run}` });
  await card.getByTestId("webhook-setup").locator("summary").click();
  await card.getByTestId("webhook-secret").getByRole("button", { name: "Generate webhook secret" }).click();
  const shown = card.getByTestId("webhook-secret-success");
  await expect(shown).toContainText(`/v1/hooks/confluence/`);
  const secret = ((await shown.textContent()) ?? "").split("will not be shown again): ")[1].split(" ·")[0];
  expect(secret.length).toBeGreaterThan(30);
  await shot(admin, "sync-04-webhook-secret-once");
  await admin.reload();
  expect(await admin.content()).not.toContain(secret);

  // 5) erin (viewer) sees freshness, no controls.
  const erin = await signedInPage(browser, `/projects/${project.id}/sources`, "erin");
  await expect(erin.getByTestId("sync-status")).toBeVisible();
  await expect(erin.getByTestId("reconcile")).toHaveCount(0);
  await shot(erin, "sync-05-viewer");
  await request.post(`${FAKE}/_fake/reset`);
});
