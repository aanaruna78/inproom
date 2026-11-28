import { spawn, type ChildProcess } from "node:child_process";

import { expect, test, type APIRequestContext, type Browser, type Page } from "@playwright/test";

/**
 * INPROOM-36 (E04-S05): the Knowledge Explorer. A project with a Confluence space (fake on :18092) and an
 * uploaded document is synced by a tenant-scoped worker; members browse sources and items, inspect evidence,
 * search, follow the graph and trace to supporting documents. Unauthorized users see nothing.
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

async function signedInPage(browser: Browser, path: string, username: string, options = {}) {
  const page = await (await browser.newContext(options)).newPage();
  await page.goto(path);
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(`${username}-local`);
  await page.locator("#kc-login").click();
  await expect(page.getByTestId("signed-in-user")).toBeVisible();
  return page;
}

let worker: ChildProcess | undefined;
test.afterAll(() => {
  worker?.kill("SIGTERM");
});

type World = { projectId: string; doc: string; alice: Record<string, string> };
let world: World | undefined;

async function setUp(request: APIRequestContext): Promise<World> {
  if (world) return world;
  expect((await request.post(`${FAKE}/_fake/reset`)).status()).toBe(204);
  const run = Date.now().toString(36).slice(-5);
  const alice = await auth(request, "alice");
  const ws = await (await request.post(`${API}/v1/workspaces`, { headers: alice, data: { slug: `kx-${run}`, name: `Knowledge ${run}` } })).json();
  const project = await (
    await request.post(`${API}/v1/workspaces/${ws.id}/projects`, {
      headers: alice,
      data: { key: `KX${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10), name: `Knowledge project ${run}` },
    })
  ).json();
  const dave = await (await request.get(`${API}/v1/me`, { headers: await auth(request, "dave") })).json();
  await request.put(`${API}/v1/projects/${project.id}/members/${dave.user_id}`, { headers: alice, data: { roles: ["developer"] } });
  const connection = await (
    await request.post(`${API}/v1/connections`, {
      headers: alice,
      data: { provider: "confluence", display_name: `Confluence ${run}`, base_url: FAKE, email: EMAIL, token: VALID_TOKEN },
    })
  ).json();
  expect(
    (await request.put(`${API}/v1/projects/${project.id}/sources/confluence/${connection.id}:ENG`, { headers: alice, data: { required_labels: ["approved"] } })).status(),
  ).toBe(200);
  const doc = await (
    await request.post(`${API}/v1/projects/${project.id}/documents`, {
      headers: alice,
      multipart: {
        file: {
          name: "invoicing-notes.md",
          mimeType: "text/markdown",
          buffer: Buffer.from("# Invoicing notes\n\nThe invoice PDF design follows /pages/101 (Architecture)."),
        },
      },
    })
  ).json();
  const tenantId = (await (await request.get(`${API}/v1/me`, { headers: alice })).json()).tenant_id;
  worker = spawn("uv", ["run", "python", "-m", "app.worker", "--poll", "1", "--tick", "1", "--tenant", tenantId], {
    cwd: "../api",
    env: { ...process.env, INPROOM_CONFLUENCE_ALLOWED_HOSTS: "*.atlassian.net,http://localhost:18092" },
    stdio: "ignore",
  });
  // Ready when the derive and graph jobs have caught up: the document's node links to the Architecture page.
  await expect(async () => {
    const r = await request.get(`${API}/v1/projects/${project.id}/graph/nodes/doc:${doc.document_id}`, { headers: alice });
    expect(r.status()).toBe(200);
    expect(JSON.stringify(await r.json())).toContain("confluence:101");
  }).toPass({ timeout: 90_000 });
  world = { projectId: project.id, doc: doc.document_id, alice };
  return world;
}

test("browse sources and items, inspect evidence and freshness", async ({ browser, request }) => {
  test.setTimeout(180_000);
  const { projectId } = await setUp(request);
  const page = await signedInPage(browser, `/projects/${projectId}`, "dave");
  await page.getByTestId("knowledge-link").click();
  await expect(page.getByRole("heading", { name: "Knowledge Explorer" })).toBeVisible();
  await expect(page.getByTestId("source-health")).toContainText("ENG");
  await expect(page.getByTestId("inspector-empty")).toBeVisible();

  // Documents (uploads): no provider location
  await page.getByRole("link", { name: "invoicing-notes.md" }).click();
  await expect(page.getByTestId("inspector")).toContainText("invoicing-notes.md");
  await expect(page.getByTestId("no-original")).toBeVisible();

  // Confluence pages: canonical revision, freshness, access scope, original link
  await page.getByRole("navigation", { name: "Item kinds" }).getByRole("link", { name: "Confluence pages" }).click();
  await page.getByTestId("item-list").getByRole("link", { name: "Architecture" }).click();
  const inspector = page.getByTestId("inspector");
  await expect(inspector.getByRole("heading", { name: "Architecture" })).toBeVisible();
  await expect(page.getByTestId("item-freshness")).toHaveText("current");
  await expect(page.getByTestId("revision-facts")).toContainText("Provider version");
  await expect(page.getByTestId("access-scope")).toContainText("trusted");
  await expect(page.getByTestId("indexed-at")).not.toHaveText("not indexed for search yet");
  await expect(page.getByTestId("open-original")).toHaveAttribute("href", /pages\/101/);
  await expect(page.getByTestId("item-list").getByRole("link", { name: "Architecture" })).toHaveAttribute("aria-current", "true");
  await expect(page.getByTestId("item-list")).not.toContainText("Leads only"); // restricted: never listed
  await shot(page, "inproom-36-explorer-inspect");
});

test("another tenant and non-members see not found", async ({ browser, request }) => {
  test.setTimeout(180_000);
  const { projectId } = await setUp(request);
  for (const user of ["erin", "bob"]) {
    const page = await signedInPage(browser, `/projects/${projectId}/knowledge`, user);
    await expect(page.getByRole("heading", { name: "Knowledge Explorer" })).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText("Architecture");
  }
});
