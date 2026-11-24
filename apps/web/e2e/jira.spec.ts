import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-133 / story INPROOM-23: connect Jira (fake Jira on :18091), select a Jira project, review the
 * discovered schema and mapping, override a field, preview normalised issues; viewers are read-only and the
 * API token never reaches the browser.
 */

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const API = "http://localhost:8000";
const FAKE = "http://localhost:18091";
const TOKEN_URL = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";
// Fake credentials known to the fake Jira (apps/api/tests/fakes/jira.py).
const EMAIL = "bot@acme.example";
const VALID_TOKEN = "ATATT3xFfGF0VALID00000000000000000000000000000000000000000000000000000000=AB12CD34";
const BAD_TOKEN = "ATATT3xFfGF0NOPE00000000000000000000000000000000000000000000000000000000=00000000";

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

test("connect Jira, select a project, review and override the mapping, preview issues", async ({ browser, request }) => {
  test.setTimeout(150_000);
  expect((await request.post(`${FAKE}/_fake/reset`)).status()).toBe(204);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (
    await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `jira-${run}`, name: `Jira ${run}` } })
  ).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `JR${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Jira project ${run}` },
    })
  ).json();
  for (const [user, role] of [
    ["dave", "project_admin"],
    ["erin", "viewer"],
  ] as const) {
    const me = await (await request.get(`${API}/v1/me`, { headers: await auth(request, user) })).json();
    await request.put(`${API}/v1/projects/${project.id}/members/${me.user_id}`, { headers: alice, data: { roles: [role] } });
  }
  const name = `Jira ${run}`;

  // 1) alice (tenant admin) connects Jira: a bad token is refused, a valid one connects; the token never comes back.
  const { page: admin, bodies } = await signedInPage(browser, "/integrations", "alice");
  const connect = admin.getByTestId("connect-jira");
  await connect.getByLabel("Name").fill(name);
  await connect.getByLabel("Site URL").fill(FAKE);
  await connect.getByLabel("Account email").fill(EMAIL);
  await connect.getByLabel("API token").fill(BAD_TOKEN);
  await connect.getByRole("button", { name: "Connect Jira" }).click();
  await expect(admin.getByTestId("connect-jira-error")).toContainText("rejected the credential");
  await expect(connect.getByLabel("API token")).toHaveValue(""); // never re-filled
  await connect.getByLabel("API token").fill(VALID_TOKEN);
  await connect.getByRole("button", { name: "Connect Jira" }).click();
  await expect(admin.getByTestId("connect-jira-success")).toContainText(`Connected ${name} as ${EMAIL}`);
  const card = admin.getByTestId("connection").filter({ hasText: name });
  await expect(card).toContainText("ACTIVE");
  await expect(card.getByTestId("fingerprint")).toHaveText("…CD34");
  await shot(admin, "jira-01-connected");

  // 2) dave (project admin) selects ABC; discovery reports its access and warnings.
  const connectionId = (await (await request.get(`${API}/v1/connections`, { headers: alice })).json()).find(
    (c: { display_name: string }) => c.display_name === name,
  ).id;
  const { page: dave, bodies: daveBodies } = await signedInPage(
    browser,
    `/projects/${project.id}/sources?jira_connection=${connectionId}`,
    "dave",
  );
  await expect(dave.getByTestId("jira-options")).not.toContainText("HID"); // not browsable
  await dave.getByTestId("jira-option").filter({ hasText: "ABC" }).getByRole("button", { name: "Select" }).click();
  // The option re-renders as "selected" (its form goes away), so assert on the selected row instead.
  await expect(dave.getByTestId("jira-option").filter({ hasText: "ABC" })).toContainText("selected");
  const row = dave.getByTestId("selected-jira-project").filter({ hasText: "ABC" });
  await expect(row).toContainText("ACTIVE");
  await expect(row).toContainText("schema v1");
  await expect(row.getByTestId("jira-warnings")).toContainText("EXCESS_PRIVILEGE");
  await expect(row.getByTestId("jira-access")).toHaveText("read + write");
  await expect(dave.getByTestId("sync-scope")).toContainText("Jira ABC (schema v1, mapping v1)");
  await shot(dave, "jira-02-selected");

  // 3) The schema and mapping: types, statuses and fields detected by schema; the preview reads through it.
  await row.getByRole("link").click();
  await expect(dave.getByTestId("mapping-story_points")).toContainText("Story point estimate (customfield_10016)");
  await expect(dave.getByTestId("mapping-acceptance_criteria")).toContainText("Acceptance Criteria (customfield_10050)");
  await expect(dave.getByTestId("issue-type-mapping")).toContainText("Sub-task");
  await expect(dave.getByTestId("status-mapping")).toContainText("Verified");
  const story = dave.locator('[data-testid="issue-row"][aria-label="ABC-2"]');
  await expect(story).toContainText("story");
  await expect(story).toContainText("blocks ABC-3");
  await expect(story.getByTestId("issue-acceptance")).toContainText("Given a customer");
  await shot(dave, "jira-03-mapping-and-preview");

  // Override: acceptance criteria is read from "Team" instead; the mapping and the preview follow.
  const form = dave.getByTestId("mapping-form");
  await form.locator('select[name="acceptance_criteria"]').selectOption("customfield_10060");
  await form.getByRole("button", { name: "Save mapping" }).click();
  await expect(dave.getByTestId("mapping-form-success")).toContainText("Mapping saved (version 2)");
  await expect(dave.getByTestId("mapping-acceptance_criteria")).toContainText("Team (customfield_10060)");
  await expect(dave.getByTestId("mapping-acceptance_criteria")).toContainText("override");
  await expect(dave.locator('[data-testid="issue-row"][aria-label="ABC-2"]').getByTestId("issue-acceptance")).toHaveText("—");
  await dave.getByTestId("rediscover").getByRole("button", { name: "Rediscover schema" }).click();
  await expect(dave.getByTestId("rediscover-success")).toContainText("No schema change (still v1)");
  await shot(dave, "jira-04-override");

  // 4) erin (viewer) sees the Jira project and its mapping, with no controls.
  const { page: erin } = await signedInPage(browser, `/projects/${project.id}/sources`, "erin");
  await expect(erin.getByTestId("jira-read-only")).toBeVisible();
  await expect(erin.getByTestId("selected-jira-project").filter({ hasText: "ABC" })).toBeVisible();
  await expect(erin.getByTestId("remove-jira-project")).toHaveCount(0);
  await erin.getByTestId("selected-jira-project").filter({ hasText: "ABC" }).getByRole("link").click();
  await expect(erin.getByTestId("field-mapping")).toBeVisible();
  await expect(erin.getByTestId("mapping-form")).toHaveCount(0);
  await expect(erin.getByTestId("rediscover")).toHaveCount(0);
  await shot(erin, "jira-05-viewer-read-only");

  // The API token never reached any browser.
  for (const text of [...bodies, ...daveBodies, await admin.content(), await dave.content(), await erin.content()]) {
    expect(text).not.toContain(VALID_TOKEN);
    expect(text).not.toContain(BAD_TOKEN);
  }
  await request.post(`${FAKE}/_fake/reset`);
});
