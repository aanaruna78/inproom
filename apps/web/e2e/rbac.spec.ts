import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-96 / story INPROOM-20: project roles end to end with two real users in two browsers.
 * alice (tenant admin) manages dave's membership; dave's view and access follow immediately.
 */

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const API = "http://localhost:8000";
const TOKEN_URL = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

/** Member row on the members screen, found by the email shown in it. */
function memberRow(page: Page, email: string) {
  return page.getByTestId("member").filter({ hasText: email });
}

async function token(request: APIRequestContext, username: string) {
  const r = await request.post(TOKEN_URL, {
    form: { grant_type: "password", client_id: "inproom-dev-cli", username, password: `${username}-local` },
  });
  return (await r.json()).access_token as string;
}

async function signedInPage(browser: Browser, path: string, username: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(path);
  await expect(page).toHaveURL(/\/realms\/inproom\/protocol\/openid-connect\/auth/);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(`${username}-local`);
  await page.locator("#kc-login").click();
  await expect(page.getByTestId("signed-in-user")).toHaveText(`${username}@tenant-a.local`);
  return page;
}

test("project admin grants, promotes and revokes access; the UI and API follow immediately", async ({
  browser,
  request,
}) => {
  test.setTimeout(120_000);
  const run = Date.now().toString(36).slice(-5);
  const key = `RB${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10);

  // Setup through the API as alice: a workspace and a project (alice becomes its project_admin).
  const alice = { Authorization: `Bearer ${await token(request, "alice")}` };
  const ws = await (
    await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `rbac-${run}`, name: `RBAC ${run}` } })
  ).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key, name: `RBAC project ${run}`, mode: "NEW" },
    })
  ).json();
  const projectUrl = `/projects/${project.id}`;

  // dave signs in once (registers him in the tenant directory) but is not a member yet.
  const dave = await signedInPage(browser, projectUrl, "dave");
  await expect(dave.getByRole("heading", { name: /404/ })).toBeVisible();
  await shot(dave, "rbac-01-dave-not-a-member-404");

  // 1) alice adds dave as viewer from the members screen.
  const admin = await signedInPage(browser, `${projectUrl}/members`, "alice");
  await expect(memberRow(admin, "alice@tenant-a.local")).toBeVisible();
  const add = admin.getByTestId("add-member");
  await add.getByLabel("Email").fill("dave@tenant-a.local");
  await add.getByLabel("Role").selectOption("viewer");
  await add.getByRole("button", { name: "Add member" }).click();
  await expect(admin.getByTestId("add-member-success")).toContainText("dave@tenant-a.local is now viewer");
  await expect(memberRow(admin, "dave@tenant-a.local")).toBeVisible();
  await shot(admin, "rbac-02-alice-added-dave-as-viewer");

  // 2) dave sees the project read-only: no edit/archive controls, members are view-only.
  await dave.goto(projectUrl);
  await expect(dave.getByTestId("my-roles")).toHaveText("viewer");
  await expect(dave.getByTestId("edit-project")).toHaveCount(0);
  await expect(dave.getByTestId("archive-project")).toHaveCount(0);
  await expect(dave.getByTestId("read-only-note")).toBeVisible();
  await shot(dave, "rbac-03-dave-viewer-read-only");
  await dave.getByTestId("members-link").click();
  await expect(dave.getByTestId("members-read-only")).toBeVisible();
  await expect(dave.getByRole("button", { name: "Save roles" })).toHaveCount(0);
  await shot(dave, "rbac-04-dave-members-read-only");

  // ...and the server refuses the hidden action anyway (direct API call with dave's token).
  const daveApi = { Authorization: `Bearer ${await token(request, "dave")}` };
  const forced = await request.patch(`${API}/v1/projects/${project.id}`, {
    headers: { ...daveApi, "If-Match": '"1"' },
    data: { name: "Renamed by a viewer" },
  });
  expect(forced.status()).toBe(403);
  expect((await forced.json()).required_permission).toBe("project.update");

  // 3) alice promotes dave to project_admin; dave can now edit.
  await admin.reload();
  const daveRow = memberRow(admin, "dave@tenant-a.local");
  await daveRow.getByLabel("project admin", { exact: true }).check();
  await daveRow.getByLabel("viewer", { exact: true }).uncheck();
  await daveRow.getByRole("button", { name: "Save roles" }).click();
  await expect(memberRow(admin, "dave@tenant-a.local").getByTestId("member-success")).toContainText("Roles saved: project_admin");
  await shot(admin, "rbac-05-alice-promoted-dave");

  await dave.goto(projectUrl);
  await expect(dave.getByTestId("my-roles")).toHaveText("project_admin");
  const edit = dave.getByTestId("edit-project");
  await edit.getByLabel("Name").fill(`Renamed by dave ${run}`);
  await edit.getByRole("button", { name: "Save changes" }).click();
  await expect(dave.getByTestId("edit-project-success")).toContainText(`Renamed by dave ${run}`);
  await expect(dave.getByRole("heading", { level: 1 })).toContainText(`Renamed by dave ${run}`);
  await shot(dave, "rbac-06-dave-admin-edits");

  // 4) alice removes dave; his very next page load is a 404 (no sign-out or token refresh needed).
  await admin.reload();
  await memberRow(admin, "dave@tenant-a.local").getByRole("button", { name: "Remove" }).click();
  await memberRow(admin, "dave@tenant-a.local").getByRole("button", { name: "Confirm remove" }).click();
  await expect(memberRow(admin, "dave@tenant-a.local")).toHaveCount(0);
  await shot(admin, "rbac-07-alice-removed-dave");
  await dave.goto(projectUrl);
  await expect(dave.getByRole("heading", { name: /404/ })).toBeVisible();
  await expect(dave.getByText(`Renamed by dave ${run}`)).toHaveCount(0);
  await shot(dave, "rbac-08-dave-removed-404");

  // 5) alice cannot remove herself while she is the last project admin.
  const aliceRow = memberRow(admin, "alice@tenant-a.local");
  await aliceRow.getByRole("button", { name: "Remove" }).click();
  await aliceRow.getByRole("button", { name: "Confirm remove" }).click();
  await expect(memberRow(admin, "alice@tenant-a.local").getByTestId("member-error")).toContainText("at least one project_admin");
  await expect(memberRow(admin, "alice@tenant-a.local").getByTestId("member-error")).toBeFocused();
  await expect(memberRow(admin, "alice@tenant-a.local")).toBeVisible();
  await shot(admin, "rbac-09-last-admin-protected");
});

test("adding someone who never signed in explains what to do", async ({ browser, request }) => {
  const alice = { Authorization: `Bearer ${await token(request, "alice")}` };
  const ws = (await (await request.get(`${API}/v1/workspaces`, { headers: alice })).json()).items[0];
  const projects = await (await request.get(`${API}/v1/workspaces/${ws.id}/projects`, { headers: alice })).json();
  test.skip(projects.items.length === 0, "tenant A has no projects yet");
  const admin = await signedInPage(browser, `/projects/${projects.items[0].id}/members`, "alice");
  const add = admin.getByTestId("add-member");
  await add.getByLabel("Email").fill("nobody@tenant-a.local");
  await add.getByRole("button", { name: "Add member" }).click();
  await expect(admin.getByTestId("add-member-error")).toContainText("has signed in to this tenant yet");
  await expect(add.getByLabel("Email")).toHaveAttribute("aria-invalid", "true");
  await shot(admin, "rbac-10-unknown-user");
});

test("a failed removal replaces an earlier save confirmation in the same row (INPROOM-101)", async ({
  browser,
  request,
}) => {
  const run = Date.now().toString(36).slice(-5);
  const alice = { Authorization: `Bearer ${await token(request, "alice")}` };
  const ws = await (
    await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `fb-${run}`, name: `FB ${run}` } })
  ).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `FB${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Feedback ${run}` },
    })
  ).json();
  const admin = await signedInPage(browser, `/projects/${project.id}/members`, "alice");
  const row = memberRow(admin, "alice@tenant-a.local");
  await row.getByLabel("reviewer", { exact: true }).check();
  await row.getByRole("button", { name: "Save roles" }).click();
  await expect(row.getByTestId("member-success")).toContainText("Roles saved: project_admin, reviewer");
  await row.getByRole("button", { name: "Remove" }).click();
  await row.getByRole("button", { name: "Confirm remove" }).click();
  await expect(row.getByTestId("member-error")).toContainText("at least one project_admin");
  await expect(row.getByTestId("member-success")).toHaveCount(0);
  await shot(admin, "rbac-11-latest-result-wins");
});
