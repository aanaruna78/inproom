import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-137 / story INPROOM-24: connect Confluence (fake on :18092), select a space with the "approved" label
 * filter, sync, browse the catalog and a page (versions, restrictions, content); viewers are read-only; the API
 * token never reaches the browser.
 */

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const API = "http://localhost:8000";
const FAKE = "http://localhost:18092";
const TOKEN_URL = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";
// Fake credentials known to the fake Confluence (apps/api/tests/fakes/confluence.py).
const EMAIL = "bot@acme.example";
const VALID_TOKEN = "ATATT3xFfGF0CONFLUENCE0000000000000000000000000000000000000000000000000=CF12AB34";
const BAD_TOKEN = "ATATT3xFfGF0CONFNOPE00000000000000000000000000000000000000000000000000000=00000000";

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

test("connect Confluence, select an approved space, sync, browse catalog and page", async ({ browser, request }) => {
  test.setTimeout(150_000);
  expect((await request.post(`${FAKE}/_fake/reset`)).status()).toBe(204);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (
    await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `conf-${run}`, name: `Conf ${run}` } })
  ).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `CF${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Confluence project ${run}` },
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

  // 1) alice connects Confluence: a bad token is refused, a valid one connects; the token never comes back.
  const { page: admin, bodies } = await signedInPage(browser, "/integrations", "alice");
  const connect = admin.getByTestId("connect-confluence");
  await connect.getByLabel("Name").fill(name);
  await connect.getByLabel("Site URL").fill(FAKE);
  await connect.getByLabel("Account email").fill(EMAIL);
  await connect.getByLabel("API token").fill(BAD_TOKEN);
  await connect.getByRole("button", { name: "Connect Confluence" }).click();
  await expect(admin.getByTestId("connect-confluence-error")).toContainText("rejected the credential");
  await expect(connect.getByLabel("API token")).toHaveValue("");
  await connect.getByLabel("API token").fill(VALID_TOKEN);
  await connect.getByRole("button", { name: "Connect Confluence" }).click();
  await expect(admin.getByTestId("connect-confluence-success")).toContainText(`Connected ${name} as ${EMAIL}`);
  const card = admin.getByTestId("connection").filter({ hasText: name });
  await expect(card).toContainText("ACTIVE");
  await expect(card.getByTestId("fingerprint")).toHaveText("…AB34");
  await shot(admin, "confluence-01-connected");

  // 2) dave selects ENG with the "approved" label; the sync reports what it catalogued.
  const connectionId = (await (await request.get(`${API}/v1/connections`, { headers: alice })).json()).find(
    (c: { display_name: string }) => c.display_name === name,
  ).id;
  const { page: dave, bodies: daveBodies } = await signedInPage(
    browser,
    `/projects/${project.id}/sources?confluence_connection=${connectionId}`,
    "dave",
  );
  await expect(dave.getByTestId("space-options")).not.toContainText("SECRET"); // not readable
  const eng = dave.locator('[data-testid="space-option"][aria-label="ENG"]');
  await eng.getByLabel("Required labels (comma-separated)").fill("approved");
  await eng.getByRole("button", { name: "Select and sync" }).click();
  await expect(dave.getByTestId("select-space-ENG-success")).toContainText("ENG synced: 3 added, 2 restricted");
  const row = dave.locator('[data-testid="selected-space"][aria-label="ENG"]');
  await expect(row).toContainText("labels approved");
  await expect(row.getByTestId("space-counts")).toContainText("3 in scope");
  await expect(row.getByTestId("space-warnings")).toContainText("EXCESS_PRIVILEGE");
  await expect(dave.getByTestId("sync-scope")).toContainText("Confluence ENG: 3 pages (labels approved)");
  await shot(dave, "confluence-02-space-selected");

  // 3) An edit in Confluence shows up on the next sync as a new version.
  await request.post(`${FAKE}/_fake/edit`, { data: { page_id: 101, body: "<h1>Architecture</h1><p>Now with queues.</p>" } });
  await row.getByTestId("sync-space").getByRole("button", { name: "Sync now" }).click();
  await expect(dave.getByTestId("sync-space-success").first()).toContainText("Synced: 1 updated, 4 unchanged");

  // 4) The catalog, filtered to restricted pages, and a page with versions, ACL and live content.
  await row.getByRole("link").click();
  await expect(dave.getByTestId("page-catalog")).toContainText("Architecture");
  await dave.getByTestId("status-filter").getByRole("link", { name: /restricted/ }).click();
  await expect(dave.locator('[data-testid="page-row"][aria-label="104"]')).toContainText("RESTRICTED");
  await dave.locator('[data-testid="page-row"][aria-label="104"]').getByRole("link").click();
  await expect(dave.getByTestId("page-acl")).toContainText("Inherited from Leads only: group bots");
  await expect(dave.getByTestId("page-not-readable")).toContainText("restricted");
  await shot(dave, "confluence-03-restricted-page");
  await dave.goto(`/projects/${project.id}/sources`);
  await dave.locator('[data-testid="selected-space"][aria-label="ENG"]').getByRole("link").click();
  await dave.locator('[data-testid="page-row"][aria-label="101"]').getByRole("link").click();
  await expect(dave.getByTestId("page-content")).toContainText("Now with queues.");
  await expect(dave.getByTestId("content-version")).toContainText("Version 4 of 4");
  await expect(dave.getByTestId("page-attachments")).toContainText("diagram.png");
  await dave.getByTestId("page-versions").getByRole("link", { name: "v3" }).click();
  await expect(dave.getByTestId("page-content")).toContainText("Svc | Owner");
  await shot(dave, "confluence-04-page-versions-content");

  // 5) erin (viewer) browses read-only.
  const { page: erin } = await signedInPage(browser, `/projects/${project.id}/sources`, "erin");
  await expect(erin.getByTestId("confluence-read-only")).toBeVisible();
  await expect(erin.getByTestId("sync-space")).toHaveCount(0);
  await expect(erin.getByTestId("remove-space")).toHaveCount(0);
  await erin.locator('[data-testid="selected-space"][aria-label="ENG"]').getByRole("link").click();
  await expect(erin.getByTestId("page-catalog")).toBeVisible();
  await expect(erin.getByTestId("sync-space")).toHaveCount(0);
  await shot(erin, "confluence-05-viewer-read-only");

  for (const text of [...bodies, ...daveBodies, await admin.content(), await dave.content(), await erin.content()]) {
    expect(text).not.toContain(VALID_TOKEN);
    expect(text).not.toContain(BAD_TOKEN);
  }
  await request.post(`${FAKE}/_fake/reset`);
});
