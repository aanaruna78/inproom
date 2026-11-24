import { spawn, type ChildProcess } from "node:child_process";

import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-148 / story INPROOM-26: a token stops working at the provider -> the worker marks the connection
 * INVALID -> Integrations shows it under Needs attention -> the admin reauthorizes -> the source is fresh again;
 * exclude / include; disconnect shows its impact first -> a new connection adopts the source; viewers read-only.
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
  const bodies: string[] = [];
  page.on("response", async (r) => {
    try {
      bodies.push(await r.text());
    } catch {
      /* no body */
    }
  });
  await page.goto(path);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(`${username}-local`);
  await page.locator("#kc-login").click();
  await expect(page.getByTestId("signed-in-user")).toHaveText(`${username}@tenant-a.local`);
  return { page, bodies };
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

test("token breaks, needs attention, reauthorize, exclude/include, disconnect with impact, adopt", async ({ browser, request }) => {
  test.setTimeout(180_000);
  expect((await request.post(`${FAKE}/_fake/reset`)).status()).toBe(204);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `hlth-${run}`, name: `Health ${run}` } })).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `HL${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Health project ${run}` },
    })
  ).json();
  for (const [user, role] of [
    ["dave", "project_admin"],
    ["erin", "viewer"],
  ] as const) {
    const me = await (await request.get(`${API}/v1/me`, { headers: await auth(request, user) })).json();
    await request.put(`${API}/v1/projects/${project.id}/members/${me.user_id}`, { headers: alice, data: { roles: [role] } });
  }
  const name = `Confluence ${run}`;
  const connection = await (
    await request.post(`${API}/v1/connections`, {
      headers: alice,
      data: { provider: "confluence", display_name: name, base_url: FAKE, email: EMAIL, token: VALID_TOKEN },
    })
  ).json();
  expect(
    (await request.put(`${API}/v1/projects/${project.id}/sources/confluence/${connection.id}:ENG`, { headers: alice, data: { required_labels: ["approved"] } })).status(),
  ).toBe(200);
  const tenantId = (await (await request.get(`${API}/v1/me`, { headers: alice })).json()).tenant_id;
  worker = spawn("uv", ["run", "python", "-m", "app.worker", "--poll", "1", "--tick", "1", "--tenant", tenantId], {
    cwd: "../api",
    env: {
      ...process.env,
      INPROOM_SYNC_INTERVAL_SECONDS: "3",
      INPROOM_HEALTH_INTERVAL_SECONDS: "60", // detection here comes from the sync hitting 401
      INPROOM_CONFLUENCE_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18092",
      INPROOM_JIRA_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18091",
      INPROOM_GIT_ALLOWED_HOSTS: "api.github.com,http://localhost:18090",
    },
    stdio: "ignore",
  });

  const { page: dave } = await signedInPage(browser, `/projects/${project.id}/sources`, "dave");
  const row = dave.locator('[data-testid="sync-row"][aria-label="confluence ENG"]');
  await eventually(dave, async () => expect(row.getByTestId("sync-health")).toHaveText("fresh", { timeout: 1_000 }));

  // 1) The token stops working at Confluence: the worker notices, the admin sees it under Needs attention.
  await request.post(`${FAKE}/_fake/token`, { data: { token: VALID_TOKEN, valid: false } });
  const { page: admin, bodies } = await signedInPage(browser, "/integrations", "alice");
  const item = admin.locator(`[data-testid="attention-item"][aria-label="${name}"]`);
  await eventually(admin, async () => expect(item).toContainText("Reauthorization needed", { timeout: 1_000 }));
  await expect(item).toContainText("INVALID");
  await shot(admin, "health-01-needs-attention");
  await eventually(dave, async () => expect(row.getByTestId("sync-health")).toHaveText("paused", { timeout: 1_000 }));
  await shot(dave, "health-02-source-paused");

  // 2) Reauthorize from the connection page: ACTIVE again, and the source catches up.
  await request.post(`${FAKE}/_fake/token`, { data: { token: VALID_TOKEN, valid: true } });
  await item.getByRole("link", { name }).click();
  await admin.getByTestId("reauthorize").getByLabel("New token").fill(VALID_TOKEN);
  await admin.getByTestId("reauthorize").getByRole("button", { name: "Reauthorize" }).click();
  await expect(admin.getByTestId("reauthorize-success")).toContainText("connection is ACTIVE");
  await shot(admin, "health-03-reauthorized");
  await eventually(dave, async () => expect(row.getByTestId("sync-health")).toHaveText("fresh", { timeout: 1_000 }));

  // 3) dave excludes the space (history kept) and includes it again.
  await row.getByTestId("exclude").getByRole("button", { name: "Exclude" }).click();
  // The row re-renders with "Include again" (its form replaces the exclude form), so assert on the new state.
  await expect(row.getByTestId("sync-health")).toHaveText("excluded");
  await expect(row.getByTestId("include")).toBeVisible();
  await expect(dave.getByTestId("sync-scope")).toContainText("ENG — excluded");
  await shot(dave, "health-04-excluded");
  await row.getByTestId("include").getByRole("button", { name: "Include again" }).click();
  await eventually(dave, async () => expect(row.getByTestId("sync-health")).toHaveText("fresh", { timeout: 1_000 }));

  // 4) Disconnect: the impact comes first; then a new connection adopts the space.
  await admin.goto(`/integrations/${connection.id}`);
  await expect(admin.getByTestId("impact-summary")).toContainText("stops syncing 1 active source(s) in 1 project(s)");
  await shot(admin, "health-05-disconnect-impact");
  await admin.getByTestId("revoke").getByLabel("Type REVOKE to confirm").fill("REVOKE");
  await admin.getByTestId("revoke").getByRole("button", { name: "Disconnect" }).click();
  await expect(admin.getByRole("heading", { level: 1 })).toContainText("REVOKED");
  const replacement = await (
    await request.post(`${API}/v1/connections`, {
      headers: alice,
      data: { provider: "confluence", display_name: `${name} new`, base_url: FAKE, email: EMAIL, token: VALID_TOKEN },
    })
  ).json();
  await admin.reload();
  await admin.getByTestId("adopt").locator('select[name="target_connection_id"]').selectOption(replacement.id);
  await admin.getByTestId("adopt").getByRole("button", { name: "Move sources" }).click();
  // The adopt section disappears once nothing is left to move; check both connections' source lists.
  await expect(admin.getByText("No project uses this connection.")).toBeVisible();
  await admin.goto(`/integrations/${replacement.id}`);
  await expect(admin.getByTestId("connection-source")).toContainText("ENG");
  await shot(admin, "health-06-adopted");
  await eventually(dave, async () => expect(row.getByTestId("sync-health")).toHaveText("fresh", { timeout: 1_000 }));

  // 5) erin (viewer) sees freshness, no controls.
  const { page: erin } = await signedInPage(browser, `/projects/${project.id}/sources`, "erin");
  await expect(erin.getByTestId("sync-status")).toBeVisible();
  await expect(erin.getByTestId("exclude")).toHaveCount(0);
  await expect(erin.getByTestId("reconcile")).toHaveCount(0);

  for (const text of [...bodies, await admin.content()]) expect(text).not.toContain(VALID_TOKEN);
  await request.post(`${FAKE}/_fake/reset`);
});
