import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-111 / story INPROOM-22: connect GitHub (fake provider on :18090), select repositories and branches,
 * see health and permission scopes; the token never reaches the browser.
 */

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const API = "http://localhost:8000";
const FAKE = "http://localhost:18090";
const TOKEN_URL = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";
// Fake tokens known to the fake GitHub (apps/api/tests/fakes/github.py).
const VALID_TOKEN = "github_pat_11VALID000000000000000000000000000000000000000000000000000000000000000ab12";
const BAD_TOKEN = "github_pat_11NOPE0000000000000000000000000000000000000000000000000000000000000000ef56";

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
      /* redirects and aborted requests have no body */
    }
  });
  await page.goto(path);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(`${username}-local`);
  await page.locator("#kc-login").click();
  await expect(page.getByTestId("signed-in-user")).toHaveText(`${username}@tenant-a.local`);
  return { page, bodies };
}

test("connect GitHub, select repositories and branches, detect lost access, revoke", async ({ browser, request }) => {
  test.setTimeout(150_000);
  expect((await request.post(`${FAKE}/_fake/reset`)).status()).toBe(204);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (
    await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `src-${run}`, name: `Sources ${run}` } })
  ).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `SR${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Sources project ${run}` },
    })
  ).json();
  for (const [user, role] of [
    ["dave", "project_admin"],
    ["erin", "viewer"],
  ] as const) {
    const me = await (await request.get(`${API}/v1/me`, { headers: await auth(request, user) })).json();
    await request.put(`${API}/v1/projects/${project.id}/members/${me.user_id}`, { headers: alice, data: { roles: [role] } });
  }
  const name = `GitHub ${run}`;

  // 1) alice (tenant admin): an invalid token is refused; a valid one connects. The token never comes back.
  const { page: admin, bodies } = await signedInPage(browser, "/integrations", "alice");
  const connect = admin.getByTestId("connect");
  await connect.getByLabel("Name").fill(name);
  await connect.getByLabel("API URL").fill(FAKE);
  await connect.getByLabel("Fine-grained token").fill(BAD_TOKEN);
  await connect.getByRole("button", { name: "Connect" }).click();
  await expect(admin.getByTestId("connect-error")).toContainText("rejected the credential");
  await expect(connect.getByLabel("Fine-grained token")).toHaveValue(""); // never re-filled
  await shot(admin, "sources-01-invalid-token");
  await connect.getByLabel("Fine-grained token").fill(VALID_TOKEN);
  await connect.getByRole("button", { name: "Connect" }).click();
  await expect(admin.getByTestId("connect-success")).toContainText(`Connected ${name} as octo-bot`);
  const card = admin.getByTestId("connection").filter({ hasText: name });
  await expect(card).toContainText("ACTIVE");
  await expect(card.getByTestId("fingerprint")).toHaveText("…ab12");
  await shot(admin, "sources-02-connected");

  // 2) dave (project admin) picks a private repo and two branches; the scope shows exactly those.
  const { page: dave, bodies: daveBodies } = await signedInPage(browser, `/projects/${project.id}/sources`, "dave");
  const connectionId = (await (await request.get(`${API}/v1/connections`, { headers: alice })).json()).find(
    (c: { display_name: string }) => c.display_name === name,
  ).id;
  await dave.goto(`/projects/${project.id}/sources?connection=${connectionId}`);
  const api = dave.getByTestId("repository-option").filter({ hasText: "acme/api" });
  await expect(api).toContainText("private");
  await expect(dave.getByTestId("repository-option").filter({ hasText: "acme/web" })).toContainText(
    "INPROOM only needs read",
  );
  await expect(dave.getByTestId("repository-options")).not.toContainText("other/secret");
  await api.getByLabel("Branches (comma-separated)").fill("main, release/1.0");
  await api.getByRole("button", { name: "Select" }).click();
  await expect(dave.getByTestId("select-acme-api-success")).toContainText("acme/api selected: main, release/1.0");
  await expect(dave.getByTestId("sync-scope")).toContainText("acme/api: main, release/1.0");
  await shot(dave, "sources-03-selected-private-repo");

  // A branch that does not exist is refused with the reason.
  const web = dave.getByTestId("repository-option").filter({ hasText: "acme/web" });
  await web.getByLabel("Branches (comma-separated)").fill("main, nope");
  await web.getByRole("button", { name: "Select" }).click();
  await expect(dave.getByTestId("select-acme-web-error")).toContainText("Branch not found: nope");

  // 3) GitHub access to acme/api is removed; Check now reports it and the repo leaves the scope.
  await request.post(`${FAKE}/_fake/access`, { data: { token: VALID_TOKEN, repo_id: 1, grant: false } });
  await admin.reload();
  await admin.getByTestId("connection").filter({ hasText: name }).getByRole("button", { name: "Check now" }).click();
  // The check calls the provider for every selected repository; allow for a cold server on the first run.
  await expect(admin.getByTestId("connection").filter({ hasText: name })).toContainText("DEGRADED", {
    timeout: 15_000,
  });
  await expect(admin.getByTestId("connection").filter({ hasText: name }).getByTestId("health-issues")).toContainText(
    "REPOSITORY_NOT_ACCESSIBLE",
  );
  await shot(admin, "sources-04-degraded");
  await dave.reload();
  await expect(dave.getByTestId("selected-repository").filter({ hasText: "acme/api" })).toContainText("PAUSED");
  await expect(dave.getByTestId("sync-scope")).toContainText("acme/api — excluded");
  await shot(dave, "sources-05-paused-out-of-scope");

  // 4) erin (viewer) sees the sources read-only.
  const { page: erin } = await signedInPage(browser, `/projects/${project.id}/sources`, "erin");
  await expect(erin.getByTestId("sources-read-only")).toBeVisible();
  await expect(erin.getByTestId("repository-options")).toHaveCount(0);
  await shot(erin, "sources-06-viewer-read-only");

  // 5) alice revokes the connection: token destroyed, everything paused.
  await admin.reload();
  const revoke = admin.getByTestId("connection").filter({ hasText: name }).getByTestId("revoke");
  await revoke.getByLabel("Type REVOKE to confirm").fill("REVOKE");
  await revoke.getByRole("button", { name: "Revoke connection" }).click();
  await expect(admin.getByTestId("connection").filter({ hasText: name })).toContainText("REVOKED");
  await expect(admin.getByTestId("connection").filter({ hasText: name }).getByTestId("fingerprint")).toHaveText("destroyed");
  await shot(admin, "sources-07-revoked");

  // The token never reached any browser: not in pages, not in any response body.
  for (const text of [...bodies, ...daveBodies, await admin.content(), await dave.content()]) {
    expect(text).not.toContain(VALID_TOKEN);
    expect(text).not.toContain(BAD_TOKEN);
  }
  await request.post(`${FAKE}/_fake/reset`);
});
