import { expect, test } from "@playwright/test";

// INPROOM-20 independent QA (author: QA). Defect INPROOM-101 (QA-20-03).
test("successful role save replaces a previous failed removal message", async ({ page, request }) => {
  const tokenUrl = "http://localhost:18080/realms/inproom/protocol/openid-connect/token";
  async function auth(user: string) {
    const response = await request.post(tokenUrl, {
      form: { grant_type: "password", client_id: "inproom-dev-cli", username: user, password: `${user}-local` },
    });
    expect(response.ok()).toBeTruthy();
    const { access_token } = await response.json();
    return { Authorization: `Bearer ${access_token}` };
  }
  const api = "http://localhost:8000";
  const alice = await auth("alice");
  const dave = await auth("dave");
  expect((await request.get(`${api}/v1/me`, { headers: dave })).ok()).toBeTruthy();
  const me = await (await request.get(`${api}/v1/me`, { headers: alice })).json();
  const directory = await (await request.get(`${api}/v1/me`, { headers: dave })).json();
  const run = Date.now().toString(36);
  const workspaceResponse = await request.post(`${api}/v1/workspaces`, {
    headers: alice, data: { slug: `qa-${run}`, name: "QA member feedback" },
  });
  expect(workspaceResponse.status()).toBe(201);
  const workspace = await workspaceResponse.json();
  const projectResponse = await request.post(`${api}/v1/workspaces/${workspace.id}/projects`, {
    headers: alice, data: { key: `QA${run.slice(-6).toUpperCase()}`, name: "QA member feedback" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();

  await page.goto(`/projects/${project.id}/members`);
  await page.locator("#username").fill("alice");
  await page.locator("#password").fill("alice-local");
  await page.locator("#kc-login").click();
  const row = page.getByTestId("member").filter({ hasText: "alice@tenant-a.local" });
  await row.getByRole("button", { name: "Remove", exact: true }).click();
  await row.getByRole("button", { name: "Confirm remove", exact: true }).click();
  await expect(row.getByTestId("member-error")).toContainText("at least one project_admin");

  // Another admin now exists, so Alice can successfully replace her own project role.
  const add = await request.put(`${api}/v1/projects/${project.id}/members/${directory.user_id}`, {
    headers: alice, data: { roles: ["project_admin"] },
  });
  expect(add.status()).toBe(200);
  await row.getByLabel("project admin", { exact: true }).uncheck();
  await row.getByLabel("viewer", { exact: true }).check();
  await row.getByRole("button", { name: "Save roles", exact: true }).click();
  await expect.poll(async () => {
    const result = await request.get(`${api}/v1/projects/${project.id}/members`, { headers: alice });
    const members = await result.json();
    return members.find((member: { user_id: string }) => member.user_id === me.user_id).roles;
  }).toEqual(["viewer"]);
  await expect(row.getByTestId("member-success")).toContainText("Roles saved: viewer");
  await expect(row.getByTestId("member-error")).toHaveCount(0);
});
