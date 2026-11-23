import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-105 / story INPROOM-21: versioned settings, production guard and lifecycle in the browser.
 * alice = tenant admin, dave = project admin (membership only), erin = viewer.
 */

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const API = "http://localhost:8000";
const TOKEN_URL = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";

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

test("settings are versioned, production needs a tenant admin, archive makes them read-only", async ({
  browser,
  request,
}) => {
  test.setTimeout(150_000);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (
    await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `set-${run}`, name: `Settings ${run}` } })
  ).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `ST${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Settings project ${run}` },
    })
  ).json();
  for (const [user, role] of [
    ["dave", "project_admin"],
    ["erin", "viewer"],
  ] as const) {
    const headers = await auth(request, user);
    const me = await (await request.get(`${API}/v1/me`, { headers })).json();
    const r = await request.put(`${API}/v1/projects/${project.id}/members/${me.user_id}`, {
      headers: alice,
      data: { roles: [role] },
    });
    expect(r.status()).toBe(200);
  }
  const settingsUrl = `/projects/${project.id}/settings`;

  // 1) dave (project admin) changes write mode and approvals; history shows v2 and its diff.
  const dave = await signedInPage(browser, settingsUrl, "dave");
  const form = dave.getByTestId("settings-form");
  await form.getByLabel("Write mode").selectOption("draft_only");
  await form.getByRole("group", { name: "Required approval scopes" }).getByLabel("test").check();
  await form.getByLabel("Change note").fill("QA must approve tests");
  await form.getByRole("button", { name: "Save settings" }).click();
  await expect(dave.getByTestId("settings-success")).toContainText(
    "Saved settings version 2. Changed: approvals.required_scopes, write_mode.",
  );
  await expect(dave.getByTestId("settings-version-2")).toContainText("QA must approve tests");
  await expect(dave.getByTestId("settings-version-2")).toContainText("Dave Member");
  await dave.getByTestId("settings-version-2").getByRole("link", { name: "v2" }).click();
  const diff = dave.getByTestId("settings-diff");
  await expect(diff.getByRole("row", { name: /write_mode/ })).toContainText("read_only");
  await expect(diff.getByRole("row", { name: /write_mode/ })).toContainText("draft_only");
  await shot(dave, "settings-01-saved-v2-with-diff");

  // 2) dave cannot turn production on: the switch is disabled and explained...
  const production = dave.getByTestId("production-settings");
  await expect(production.getByLabel("Allow production actions")).toBeDisabled();
  await expect(production).toContainText("Only a tenant admin can enable production actions");
  // ...and the API refuses it anyway.
  const daveApi = await auth(request, "dave");
  const forced = await request.put(`${API}/v1/projects/${project.id}/settings`, {
    headers: { ...daveApi, "If-Match": '"2"' },
    data: { settings: { production: { actions_enabled: true, environments: ["prod"] }, approvals: { required_scopes: ["release"] } } },
  });
  expect(forced.status()).toBe(403);
  expect((await forced.json()).required_permission).toBe("project.production.enable");
  await shot(dave, "settings-02-project-admin-production-locked");

  // 3) Someone else saves meanwhile: dave's stale form gets a clear conflict message.
  const bump = await request.put(`${API}/v1/projects/${project.id}/settings`, {
    headers: { ...alice, "If-Match": '"2"' },
    data: { settings: { write_mode: "draft_only", approvals: { required_scopes: ["plan", "test"], expiry_hours: 24 } } },
  });
  expect(bump.status()).toBe(200);
  await form.getByLabel("Model policy").selectOption("local_only");
  await form.getByRole("button", { name: "Save settings" }).click();
  await expect(dave.getByTestId("settings-error")).toContainText("Someone else saved these settings");
  await expect(dave.getByTestId("settings-error")).toBeFocused();
  await shot(dave, "settings-03-stale-save-conflict");

  // 4) alice (tenant admin) enables production with an environment and the release scope.
  const admin = await signedInPage(browser, settingsUrl, "alice");
  const aform = admin.getByTestId("settings-form");
  await aform.getByRole("group", { name: "Required approval scopes" }).getByLabel("release").check();
  await aform.getByLabel("Allow production actions").check();
  await aform.getByLabel("Environments (comma-separated)").fill("prod");
  await aform.getByRole("button", { name: "Save settings" }).click();
  await expect(admin.getByTestId("settings-success")).toContainText("Saved settings version 4");
  await expect(admin.getByTestId("production-enabled-by")).toContainText("Production actions enabled by Alice");
  await shot(admin, "settings-04-tenant-admin-enabled-production");

  // 5) erin (viewer) sees a read-only summary.
  const erin = await signedInPage(browser, settingsUrl, "erin");
  await expect(erin.getByTestId("settings-form")).toHaveCount(0);
  await expect(erin.getByTestId("settings-read-only")).toContainText("on (prod)");
  await expect(erin.getByTestId("settings-history")).toContainText("v4");
  await shot(erin, "settings-05-viewer-read-only");

  // 6) dave archives the project: settings become read-only; restoring makes them editable again.
  await dave.goto(`/projects/${project.id}`);
  const archive = dave.getByTestId("lifecycle-archive");
  await archive.getByLabel("Reason (optional)").fill("pilot paused");
  await archive.getByRole("button", { name: "Archive project" }).click();
  // The page re-renders in the new state: status chip changes and only "restore" is offered.
  await expect(dave.getByRole("heading", { level: 1 })).toContainText("ARCHIVED");
  await expect(dave.getByTestId("lifecycle-restore")).toBeVisible();
  await expect(dave.getByTestId("edit-project")).toHaveCount(0);
  await dave.goto(settingsUrl);
  await expect(dave.getByTestId("settings-archived")).toBeVisible();
  await expect(dave.getByTestId("settings-form")).toHaveCount(0);
  await shot(dave, "settings-06-archived-read-only");
  await dave.goto(`/projects/${project.id}`);
  await dave.getByTestId("lifecycle-restore").getByRole("button", { name: "Restore project" }).click();
  await expect(dave.getByRole("heading", { level: 1 })).toContainText("ACTIVE");
  await expect(dave.getByTestId("lifecycle-archive")).toBeVisible();
  await dave.goto(settingsUrl);
  await expect(dave.getByTestId("settings-form")).toBeVisible();
  await shot(dave, "settings-07-restored-editable");
});
