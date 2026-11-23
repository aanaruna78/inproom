import { expect, test, type Page } from "@playwright/test";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;

async function shot(page: Page, name: string) {
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

async function keycloakLogin(page: Page, username: string, password: string) {
  await expect(page).toHaveURL(/\/realms\/inproom\/protocol\/openid-connect\/auth/);
  // Keycloak's login theme uses stable element ids.
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
}

test("unauthenticated users are redirected to Keycloak", async ({ page }) => {
  await page.goto("/workspaces");
  await expect(page).toHaveURL(/localhost:18080\/realms\/inproom\/protocol\/openid-connect\/auth/);
  const url = new URL(page.url());
  expect(url.searchParams.get("client_id")).toBe("inproom-web");
  expect(url.searchParams.get("code_challenge_method")).toBe("S256");
  await shot(page, "01-keycloak-login");
});

test("tenant admin creates a workspace and a project, duplicate key shows inline error", async ({ page }) => {
  const run = Date.now().toString(36).slice(-5);
  const slug = `e2e-${run}`;
  const key = `E2E${run.toUpperCase().replace(/[^A-Z0-9]/g, "")}`.slice(0, 10);

  await page.goto("/workspaces");
  await keycloakLogin(page, "alice", "alice-local");
  await expect(page).toHaveURL(/\/workspaces$/);
  await expect(page.getByTestId("signed-in-user")).toHaveText("alice@tenant-a.local");
  await shot(page, "02-workspaces-after-login");

  // Create a workspace.
  const wsForm = page.getByTestId("create-workspace");
  await wsForm.getByLabel("Name").fill(`E2E workspace ${run}`);
  await wsForm.getByLabel("Slug").fill(slug);
  await wsForm.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByTestId("create-workspace-success")).toContainText(`E2E workspace ${run}`);
  const wsLink = page.getByTestId("workspace-table").getByRole("link", { name: `E2E workspace ${run}` });
  await expect(wsLink).toBeVisible();
  await shot(page, "03-workspace-created");

  // Open it and create a project.
  await wsLink.click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(`E2E workspace ${run}`);
  const pjForm = page.getByTestId("create-project");
  await pjForm.getByLabel("Key").fill(key);
  await pjForm.getByLabel("Name").fill("End-to-end project");
  await pjForm.getByLabel("Project type").selectOption("NEW");
  await pjForm.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByTestId("create-project-success")).toContainText(`Project ${key} created (ACTIVE)`);
  const row = page.getByTestId("project-table").getByRole("row", { name: new RegExp(key) });
  await expect(row).toContainText("End-to-end project");
  await expect(row).toContainText("ACTIVE");
  await shot(page, "04-project-created");

  // Duplicate key: inline API error, input preserved, field marked invalid, focus on the alert.
  await pjForm.getByLabel("Key").fill(key);
  await pjForm.getByLabel("Name").fill("Duplicate attempt");
  await pjForm.getByRole("button", { name: "Create project" }).click();
  const alert = page.getByTestId("create-project-error");
  await expect(alert).toContainText(`Project key '${key}' already exists`);
  await expect(alert).toBeFocused();
  await expect(pjForm.getByLabel("Key")).toHaveValue(key);
  await expect(pjForm.getByLabel("Name")).toHaveValue("Duplicate attempt");
  await expect(pjForm.getByLabel("Key")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByTestId("project-table").getByRole("row", { name: new RegExp(key) })).toHaveCount(1);
  await shot(page, "05-duplicate-key-error");

  // Project detail.
  await row.getByRole("link", { name: key }).click();
  await expect(page.getByTestId("project-facts")).toContainText("project_admin");
  await expect(page.getByText("read_only")).toBeVisible();
  await shot(page, "06-project-detail");
});

test("a developer cannot create a workspace and sees the API's reason", async ({ page }) => {
  await page.goto("/workspaces");
  await keycloakLogin(page, "dave", "dave-local");
  const form = page.getByTestId("create-workspace");
  await form.getByLabel("Name").fill("Not allowed");
  await form.getByLabel("Slug").fill(`dave-${Date.now().toString(36).slice(-5)}`);
  await form.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByTestId("create-workspace-error")).toContainText("Missing permission: tenant.workspace.create");
  await shot(page, "07-developer-forbidden");
});

test("tenant B cannot open tenant A's project", async ({ page, request }) => {
  // Use the API directly as alice to find one of tenant A's projects.
  const tokenResponse = await request.post("http://localhost:18080/realms/inproom/protocol/openid-connect/token", {
    form: { grant_type: "password", client_id: "inproom-dev-cli", username: "alice", password: "alice-local" },
  });
  const { access_token } = await tokenResponse.json();
  const auth = { Authorization: `Bearer ${access_token}` };
  const ws = (await (await request.get("http://localhost:8000/v1/workspaces", { headers: auth })).json()).items[0];
  const projects = await (await request.get(`http://localhost:8000/v1/workspaces/${ws.id}/projects`, { headers: auth })).json();
  test.skip(projects.items.length === 0, "tenant A has no projects yet");

  await page.goto(`/projects/${projects.items[0].id}`);
  await keycloakLogin(page, "bob", "bob-local");
  await expect(page.getByRole("heading", { name: /404/ })).toBeVisible();
  await expect(page.getByText(projects.items[0].name)).toHaveCount(0);
  await shot(page, "08-cross-tenant-404");
});

test("login ignores off-site return URLs (no open redirect)", async ({ page }) => {
  await page.goto("/login?callbackUrl=https://evil.example/steal");
  await keycloakLogin(page, "alice", "alice-local");
  await expect(page).toHaveURL("http://localhost:3000/workspaces");
});
