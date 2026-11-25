"use server";

import { revalidatePath } from "next/cache";

import { apiClient, asProblem } from "@/lib/api";
import type { FormState } from "@/lib/form-state";

const FIELD_BY_CODE: Record<string, string> = {
  WORKSPACE_SLUG_EXISTS: "slug",
  PROJECT_KEY_EXISTS: "key",
};

function values(form: FormData, names: string[]): Record<string, string> {
  return Object.fromEntries(names.map((n) => [n, String(form.get(n) ?? "")]));
}

export async function createWorkspace(_prev: FormState, form: FormData): Promise<FormState> {
  const submitted = values(form, ["slug", "name"]);
  const api = await apiClient("/workspaces");
  const { data, error, response } = await api.POST("/v1/workspaces", {
    body: { slug: submitted.slug, name: submitted.name },
    params: { header: { "Idempotency-Key": String(form.get("idempotency_key")) } },
  });
  if (error || !data) {
    const p = asProblem(error, response.status);
    return { status: "error", message: p.detail ?? p.title, code: p.code, field: FIELD_BY_CODE[p.code],
      values: submitted, traceId: p.trace_id };
  }
  revalidatePath("/workspaces");
  return { status: "success", message: `Workspace "${data.name}" created.` };
}

export async function createProject(_prev: FormState, form: FormData): Promise<FormState> {
  const workspaceId = String(form.get("workspace_id"));
  const submitted = values(form, ["key", "name", "description", "mode"]);
  const api = await apiClient(`/workspaces/${workspaceId}/projects`);
  const { data, error, response } = await api.POST("/v1/workspaces/{workspace_id}/projects", {
    params: {
      path: { workspace_id: workspaceId },
      header: { "Idempotency-Key": String(form.get("idempotency_key")) },
    },
    body: {
      key: submitted.key,
      name: submitted.name,
      description: submitted.description || null,
      mode: submitted.mode === "MIGRATION" ? "MIGRATION" : "NEW",
      process_package: "sdlc@1",
    },
  });
  if (error || !data) {
    const p = asProblem(error, response.status);
    return { status: "error", message: p.detail ?? p.title, code: p.code, field: FIELD_BY_CODE[p.code],
      values: submitted, traceId: p.trace_id };
  }
  revalidatePath(`/workspaces/${workspaceId}/projects`);
  return { status: "success", message: `Project ${data.key} created (${data.status}).` };
}

// ---------------------------------------------------------------- project edit / archive (INPROOM-96)

function problemState(error: unknown, status: number, submitted?: Record<string, string>): FormState {
  const p = asProblem(error, status);
  const message =
    p.code === "FORBIDDEN"
      ? `You don't have permission to do this (requires ${p.required_permission ?? "a higher role"}).`
      : (p.detail ?? p.title);
  return { status: "error", message, code: p.code, values: submitted, traceId: p.trace_id };
}

export async function updateProject(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const submitted = values(form, ["name", "description"]);
  const api = await apiClient(`/projects/${projectId}`);
  const { data, error, response } = await api.PATCH("/v1/projects/{project_id}", {
    params: { path: { project_id: projectId }, header: { "If-Match": `"${form.get("version")}"` } },
    body: { name: submitted.name, description: submitted.description || null },
  });
  if (error || !data) return problemState(error, response.status, submitted);
  revalidatePath(`/projects/${projectId}`);
  return { status: "success", message: `Project renamed to "${data.name}".` };
}

const LIFECYCLE_DONE: Record<string, string> = {
  activate: "Project activated.",
  archive: "Project archived. It is now read-only.",
  restore: "Project restored.",
};

export async function changeLifecycle(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const action = String(form.get("action")) as "activate" | "archive" | "restore";
  const submitted = values(form, ["reason"]);
  const api = await apiClient(`/projects/${projectId}`);
  const { data, error, response } = await api.POST("/v1/projects/{project_id}/lifecycle", {
    params: { path: { project_id: projectId } },
    body: { action, reason: submitted.reason || null },
  });
  if (error || !data) return problemState(error, response.status, submitted);
  revalidatePath(`/projects/${projectId}`);
  revalidatePath(`/projects/${projectId}/settings`);
  return { status: "success", message: LIFECYCLE_DONE[action] };
}

// ---------------------------------------------------------------- settings (INPROOM-105)

type FieldError = { loc?: (string | number)[]; msg?: string };

function settingsFromForm(form: FormData) {
  const list = (name: string) => form.getAll(name).map(String);
  const num = (name: string) => Number(form.get(name));
  return {
    process_package: String(form.get("process_package")),
    write_mode: String(form.get("write_mode")),
    model_policy: String(form.get("model_policy")),
    approvals: {
      required_scopes: list("approvals.required_scopes"),
      min_approvers: num("approvals.min_approvers"),
      expiry_hours: num("approvals.expiry_hours"),
    },
    sources: {
      allowed_kinds: list("sources.allowed_kinds"),
      max_classification: String(form.get("sources.max_classification")),
      approve_new_sources: form.get("sources.approve_new_sources") === "on",
    },
    production: {
      actions_enabled: form.get("production.actions_enabled") === "on",
      environments: String(form.get("production.environments") ?? "")
        .split(",")
        .map((e) => e.trim())
        .filter(Boolean),
    },
  };
}

export async function saveSettings(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const api = await apiClient(`/projects/${projectId}/settings`);
  const { data, error, response } = await api.PUT("/v1/projects/{project_id}/settings", {
    params: { path: { project_id: projectId }, header: { "If-Match": `"${form.get("version")}"` } },
    // The API validates every field; the form only shapes the request.
    body: { settings: settingsFromForm(form) as never, change_note: String(form.get("change_note") ?? "") || null },
  });
  if (error || !data) {
    const state = problemState(error, response.status);
    if (state.code === "PRECONDITION_FAILED") {
      state.message = "Someone else saved these settings while you were editing. Reload to see the latest version.";
    }
    const errors = (error as { errors?: FieldError[] } | undefined)?.errors;
    if (errors?.length) {
      const where = (e: FieldError) => (e.loc ?? []).filter((l) => l !== "body" && l !== "settings").join(".");
      state.message = `${state.message}: ${errors.map((e) => `${where(e)} - ${e.msg}`).join("; ")}`;
    }
    return state;
  }
  revalidatePath(`/projects/${projectId}`);
  revalidatePath(`/projects/${projectId}/settings`);
  const changed = data.changed_paths.length ? ` Changed: ${data.changed_paths.join(", ")}.` : " Nothing changed.";
  return { status: "success", message: `Saved settings version ${data.version}.${changed}` };
}

// ---------------------------------------------------------------- members (INPROOM-96)

export async function addMember(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const submitted = values(form, ["email", "role"]);
  const email = submitted.email.trim().toLowerCase();
  const api = await apiClient(`/projects/${projectId}/members`);
  const users = await api.GET("/v1/users", { params: { query: { q: email, limit: 20 } } });
  const user = users.data?.items.find((u) => (u.email ?? "").toLowerCase() === email);
  if (!user) {
    return {
      status: "error",
      message: `No one with email ${email} has signed in to this tenant yet. Ask them to sign in once, then add them.`,
      field: "email",
      values: submitted,
    };
  }
  const { data, error, response } = await api.PUT("/v1/projects/{project_id}/members/{user_id}", {
    params: { path: { project_id: projectId, user_id: user.user_id } },
    body: { roles: [submitted.role as never] },
  });
  if (error || !data) return problemState(error, response.status, submitted);
  revalidatePath(`/projects/${projectId}/members`);
  return { status: "success", message: `${data.email ?? data.user_id} is now ${data.roles.join(", ")}.` };
}

export async function setMemberRoles(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const userId = String(form.get("user_id"));
  const roles = form.getAll("roles").map(String);
  if (roles.length === 0) {
    return { status: "error", message: "Pick at least one role, or remove the member instead." };
  }
  const api = await apiClient(`/projects/${projectId}/members`);
  const { data, error, response } = await api.PUT("/v1/projects/{project_id}/members/{user_id}", {
    params: { path: { project_id: projectId, user_id: userId } },
    body: { roles: roles as never[] },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/members`);
  return { status: "success", message: `Roles saved: ${data.roles.join(", ")}.` };
}

export async function removeMember(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const userId = String(form.get("user_id"));
  const api = await apiClient(`/projects/${projectId}/members`);
  const { error, response } = await api.DELETE("/v1/projects/{project_id}/members/{user_id}", {
    params: { path: { project_id: projectId, user_id: userId } },
  });
  if (error || response.status !== 204) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/members`);
  return { status: "success", message: "Member removed." };
}

// ---------------------------------------------------------------- Git connections (INPROOM-111)
// Tokens are passed straight to the API and never echoed back in form state.

export async function createConnection(_prev: FormState, form: FormData): Promise<FormState> {
  const submitted = values(form, ["display_name", "base_url"]);
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.POST("/v1/connections", {
    body: {
      provider: "github",
      display_name: submitted.display_name,
      base_url: submitted.base_url || "https://api.github.com",
      token: String(form.get("token") ?? ""),
    },
    params: { header: { "Idempotency-Key": String(form.get("idempotency_key")) } },
  });
  if (error || !data) {
    const state = problemState(error, response.status, submitted);
    state.field = state.code === "BASE_URL_NOT_ALLOWED" ? "base_url" : state.code === "CREDENTIAL_INVALID" ? "token" : undefined;
    return state;
  }
  revalidatePath("/integrations");
  return { status: "success", message: `Connected ${data.display_name} as ${data.account_login}.` };
}

export async function checkConnection(_prev: FormState, form: FormData): Promise<FormState> {
  const id = String(form.get("connection_id"));
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.POST("/v1/connections/{connection_id}/check", {
    params: { path: { connection_id: id } },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath("/integrations");
  revalidatePath(`/integrations/${id}`);
  return { status: "success", message: `Health: ${data.health?.status ?? data.status}.` };
}

export async function rotateConnection(_prev: FormState, form: FormData): Promise<FormState> {
  const id = String(form.get("connection_id"));
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.PUT("/v1/connections/{connection_id}/credential", {
    params: { path: { connection_id: id } },
    body: { token: String(form.get("token") ?? "") },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath("/integrations");
  revalidatePath(`/integrations/${id}`);
  // Reauthorization (INPROOM-146): the API checked health and resumed what it could.
  const names = (xs: { name?: unknown; project_key?: unknown }[]) => xs.map((x) => `${x.project_key}/${x.name}`).join(", ");
  const resumed = (data.resumed ?? []) as { name?: unknown; project_key?: unknown }[];
  const blocked = (data.still_blocked ?? []) as { name?: unknown; project_key?: unknown }[];
  let message = `Token rotated (${data.credential_fingerprint}); connection is ${data.status}.`;
  if (resumed.length) message += ` Resumed ${resumed.length}: ${names(resumed)}.`;
  if (blocked.length) message += ` Still blocked ${blocked.length}: ${names(blocked)}.`;
  return { status: "success", message };
}

export async function revokeConnection(_prev: FormState, form: FormData): Promise<FormState> {
  const id = String(form.get("connection_id"));
  if (String(form.get("confirm")).trim() !== "REVOKE") {
    return { status: "error", message: "Type REVOKE to confirm.", field: "confirm" };
  }
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.POST("/v1/connections/{connection_id}/revoke", {
    params: { path: { connection_id: id } },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath("/integrations");
  revalidatePath(`/integrations/${id}`);
  return { status: "success", message: "Connection revoked; its stored token was destroyed." };
}

// ---------------------------------------------------------------- project sources (INPROOM-111)

export async function selectRepository(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const connectionId = String(form.get("connection_id"));
  const repoId = String(form.get("repo_id"));
  const submitted = values(form, ["branches"]);
  const branches = submitted.branches
    .split(",")
    .map((b) => b.trim())
    .filter(Boolean);
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { data, error, response } = await api.PUT(
    "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}",
    {
      params: { path: { project_id: projectId, connection_id: connectionId, repo_id: repoId } },
      body: { branches, register_webhook: form.get("register_webhook") === "on" },
    },
  );
  if (error || !data) return { ...problemState(error, response.status, submitted), field: "branches" };
  revalidatePath(`/projects/${projectId}/sources`);
  return { status: "success", message: `${data.full_name} selected: ${data.branches.join(", ")}.` };
}

export async function removeRepository(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { error, response } = await api.DELETE(
    "/v1/projects/{project_id}/sources/repositories/{connection_id}/{repo_id}",
    {
      params: {
        path: {
          project_id: projectId,
          connection_id: String(form.get("connection_id")),
          repo_id: String(form.get("repo_id")),
        },
      },
    },
  );
  if (error || response.status !== 204) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources`);
  return { status: "success", message: "Repository removed from the project." };
}

// ---------------------------------------------------------------- Jira (INPROOM-133 / story INPROOM-23)
// The API token goes straight to the API and is never echoed back in form state.

export async function createJiraConnection(_prev: FormState, form: FormData): Promise<FormState> {
  const submitted = values(form, ["display_name", "base_url", "email"]);
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.POST("/v1/connections", {
    body: {
      provider: "jira",
      display_name: submitted.display_name,
      base_url: submitted.base_url,
      email: submitted.email.trim() || null,
      token: String(form.get("token") ?? ""),
    },
    params: { header: { "Idempotency-Key": String(form.get("idempotency_key")) } },
  });
  if (error || !data) {
    const state = problemState(error, response.status, submitted);
    state.field = state.code === "BASE_URL_NOT_ALLOWED" ? "base_url" : state.code === "CREDENTIAL_INVALID" ? "token" : undefined;
    return state;
  }
  revalidatePath("/integrations");
  return { status: "success", message: `Connected ${data.display_name} as ${data.account_login}.` };
}

export async function selectJiraProject(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const ref = `${String(form.get("connection_id"))}:${String(form.get("jira_key"))}`;
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { data, error, response } = await api.PUT("/v1/projects/{project_id}/sources/jira/{ref}", {
    params: { path: { project_id: projectId, ref } },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources`);
  const warnings = data.warnings.map((w) => (w as { code: string }).code);
  return {
    status: "success",
    message: `${data.jira_key} selected (schema v${data.snapshot_version})${warnings.length ? `; warnings: ${warnings.join(", ")}` : ""}.`,
  };
}

export async function removeJiraProject(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const ref = `${String(form.get("connection_id"))}:${String(form.get("jira_key"))}`;
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { error, response } = await api.DELETE("/v1/projects/{project_id}/sources/jira/{ref}", {
    params: { path: { project_id: projectId, ref } },
  });
  if (error || response.status !== 204) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources`);
  return { status: "success", message: "Jira project removed from the project." };
}

export async function rediscoverJira(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const sourceId = String(form.get("source_id"));
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { data, error, response } = await api.POST("/v1/projects/{project_id}/sources/jira/{source_id}/discover", {
    params: { path: { project_id: projectId, source_id: sourceId } },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources/jira/${sourceId}`);
  if (!data.changed) return { status: "success", message: `No schema change (still v${data.snapshot_version}).` };
  const sections = Object.keys(data.diff).join(", ");
  return { status: "success", message: `Schema changed: now v${data.snapshot_version} (${sections}).` };
}

const AUTO = "__auto__";
const NONE = "__none__";

export async function saveJiraMapping(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const sourceId = String(form.get("source_id"));
  // Issue type and status overrides are not edited here; keep the ones already saved.
  const kept = JSON.parse(String(form.get("kept_overrides") ?? "{}")) as {
    issue_types?: Record<string, string>;
    statuses?: Record<string, string>;
  };
  const fields: Record<string, string | null> = {};
  for (const name of ["story_points", "sprint", "epic_link", "acceptance_criteria"]) {
    const choice = String(form.get(name) ?? AUTO);
    if (choice === NONE) fields[name] = null;
    else if (choice !== AUTO) fields[name] = choice;
  }
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { data, error, response } = await api.PUT("/v1/projects/{project_id}/sources/jira/{source_id}/mapping", {
    params: {
      path: { project_id: projectId, source_id: sourceId },
      header: { "If-Match": `"${String(form.get("mapping_version"))}"` },
    },
    body: {
      fields: fields as never,
      issue_types: (kept.issue_types ?? {}) as never,
      statuses: (kept.statuses ?? {}) as never,
    },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources/jira/${sourceId}`);
  return { status: "success", message: `Mapping saved (version ${data.mapping_version}).` };
}

// ---------------------------------------------------------------- Confluence (INPROOM-137 / story INPROOM-24)

export async function createConfluenceConnection(_prev: FormState, form: FormData): Promise<FormState> {
  const submitted = values(form, ["display_name", "base_url", "email"]);
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.POST("/v1/connections", {
    body: {
      provider: "confluence",
      display_name: submitted.display_name,
      base_url: submitted.base_url,
      email: submitted.email.trim() || null,
      token: String(form.get("token") ?? ""),
    },
    params: { header: { "Idempotency-Key": String(form.get("idempotency_key")) } },
  });
  if (error || !data) {
    const state = problemState(error, response.status, submitted);
    state.field = state.code === "BASE_URL_NOT_ALLOWED" ? "base_url" : state.code === "CREDENTIAL_INVALID" ? "token" : undefined;
    return state;
  }
  revalidatePath("/integrations");
  return { status: "success", message: `Connected ${data.display_name} as ${data.account_login}.` };
}

function list(value: FormDataEntryValue | null): string[] {
  return String(value ?? "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

function syncSummary(counts: Record<string, number>, truncated: boolean): string {
  const parts = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${k}`);
  return `${parts.join(", ") || "nothing to do"}${truncated ? " (truncated: narrow the filters)" : ""}`;
}

export async function selectConfluenceSpace(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const ref = `${String(form.get("connection_id"))}:${String(form.get("space_key"))}`;
  const submitted = values(form, ["root_page_ids", "required_labels"]);
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { data, error, response } = await api.PUT("/v1/projects/{project_id}/sources/confluence/{ref}", {
    params: { path: { project_id: projectId, ref } },
    body: {
      root_page_ids: list(form.get("root_page_ids")),
      required_labels: list(form.get("required_labels")),
      include_restricted: form.get("include_restricted") === "on",
    },
  });
  if (error || !data) return problemState(error, response.status, submitted);
  revalidatePath(`/projects/${projectId}/sources`);
  return { status: "success", message: `${data.source.space_key} synced: ${syncSummary(data.counts, data.truncated)}.` };
}

export async function removeConfluenceSpace(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const ref = `${String(form.get("connection_id"))}:${String(form.get("space_key"))}`;
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { error, response } = await api.DELETE("/v1/projects/{project_id}/sources/confluence/{ref}", {
    params: { path: { project_id: projectId, ref } },
  });
  if (error || response.status !== 204) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources`);
  return { status: "success", message: "Space removed from the project; its catalog was deleted." };
}

export async function syncConfluenceSpace(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const sourceId = String(form.get("source_id"));
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { data, error, response } = await api.POST("/v1/projects/{project_id}/sources/confluence/{source_id}/sync", {
    params: { path: { project_id: projectId, source_id: sourceId } },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources`);
  revalidatePath(`/projects/${projectId}/sources/confluence/${sourceId}`);
  return { status: "success", message: `Synced: ${syncSummary(data.counts, data.truncated)}.` };
}

// ---------------------------------------------------------------- sync status + webhooks (INPROOM-144 / INPROOM-25)

export async function reconcileSource(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const kind = String(form.get("kind")) as "git" | "jira" | "confluence";
  const api = await apiClient(`/projects/${projectId}/sources`);
  const { data, error, response } = await api.POST("/v1/projects/{project_id}/sources/{kind}/{source_id}/reconcile", {
    params: { path: { project_id: projectId, kind, source_id: String(form.get("source_id")) } },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources`);
  return {
    status: "success",
    message: data.triggers > 1 ? `Already queued (${data.triggers} requests share one job).` : "Reconcile queued.",
  };
}

export async function generateWebhookSecret(_prev: FormState, form: FormData): Promise<FormState> {
  const id = String(form.get("connection_id"));
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.POST("/v1/connections/{connection_id}/webhook-secret", {
    params: { path: { connection_id: id } },
  });
  if (error || !data) return problemState(error, response.status);
  // Shown once, in this response only: it is never stored in the page or retrievable again.
  return {
    status: "success",
    message: `Webhook URL: ${data.url ?? `<your INPROOM URL>${data.path}`} · Secret (copy it now, it will not be shown again): ${data.secret} · Signature header: ${data.signature_header}`,
  };
}

// ---------------------------------------------------------------- health + exclusion (INPROOM-147 / INPROOM-26)

export async function setSourceExcluded(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const kind = String(form.get("kind")) as "git" | "jira" | "confluence";
  const exclude = String(form.get("action")) === "exclude";
  const api = await apiClient(`/projects/${projectId}/sources`);
  const path = { params: { path: { project_id: projectId, kind, source_id: String(form.get("source_id")) } } };
  const { data, error, response } = exclude
    ? await api.POST("/v1/projects/{project_id}/sources/{kind}/{source_id}/exclude", path)
    : await api.POST("/v1/projects/{project_id}/sources/{kind}/{source_id}/include", path);
  if (error || !data) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/sources`);
  return {
    status: "success",
    message: exclude
      ? "Excluded from the sync scope. Its catalog and history are kept; include it again any time."
      : "Included again; a reconcile is queued to catch up.",
  };
}

export async function adoptSources(_prev: FormState, form: FormData): Promise<FormState> {
  const target = String(form.get("target_connection_id"));
  const from = String(form.get("from_connection_id"));
  if (!target) return { status: "error", message: "Choose the connection that should take over.", field: "target_connection_id" };
  const api = await apiClient("/integrations");
  const { data, error, response } = await api.POST("/v1/connections/{connection_id}/adopt", {
    params: { path: { connection_id: target } },
    body: { from_connection_id: from },
  });
  if (error || !data) return problemState(error, response.status);
  revalidatePath("/integrations");
  revalidatePath(`/integrations/${from}`);
  revalidatePath(`/integrations/${target}`);
  const list = (xs: { project_key: string; name: string }[]) => xs.map((x) => `${x.project_key}/${x.name}`).join(", ");
  let message = `Adopted ${data.adopted.length}${data.adopted.length ? `: ${list(data.adopted)}` : ""}.`;
  if (data.still_blocked.length) message += ` Still blocked ${data.still_blocked.length}: ${list(data.still_blocked)}.`;
  if (data.kept_excluded.length) message += ` Kept excluded ${data.kept_excluded.length}.`;
  return { status: "success", message };
}

// ---------------------------------------------------------------- migration projects (INPROOM-153 / INPROOM-19)

export async function saveMigrationPlan(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const submitted = values(form, ["jira_source_id", "history_since"]);
  const version = String(form.get("version") ?? "");
  const api = await apiClient(`/projects/${projectId}/migration`);
  const { data, error, response } = await api.PUT("/v1/projects/{project_id}/migration", {
    params: { path: { project_id: projectId }, header: version ? { "If-Match": `"${version}"` } : {} } as never,
    body: {
      jira_source_id: submitted.jira_source_id,
      import_mode: "jira_authoritative",
      history_since: submitted.history_since || null,
    },
  });
  if (error || !data) return problemState(error, response.status, submitted);
  revalidatePath(`/projects/${projectId}/migration`);
  return { status: "success", message: `Plan saved: ${data.plan?.jira_key} stays authoritative. Next: scan it.` };
}

export async function startMigrationScan(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const api = await apiClient(`/projects/${projectId}/migration`);
  const { error, response } = await api.POST("/v1/projects/{project_id}/migration/scan", {
    params: { path: { project_id: projectId } },
  });
  if (error) return problemState(error, response.status);
  revalidatePath(`/projects/${projectId}/migration`);
  return { status: "success", message: "Scan started. It runs in the background; refresh to see progress." };
}

export async function confirmMigrationBaseline(_prev: FormState, form: FormData): Promise<FormState> {
  const projectId = String(form.get("project_id"));
  const api = await apiClient(`/projects/${projectId}/migration`);
  const { data, error, response } = await api.POST("/v1/projects/{project_id}/migration/confirm", {
    params: { path: { project_id: projectId }, header: { "If-Match": `"${String(form.get("version"))}"` } } as never,
  });
  if (error || !data) {
    const state = problemState(error, response.status);
    if (state.code === "PRECONDITION_FAILED") state.message = "The plan changed while you were reviewing. Reload and check again.";
    return state;
  }
  revalidatePath(`/projects/${projectId}/migration`);
  revalidatePath(`/projects/${projectId}`);
  const baseline = (data.plan?.baseline ?? {}) as { counts?: { issues?: number } };
  return {
    status: "success",
    message: `Baseline confirmed (${baseline.counts?.issues ?? 0} issues). The project is ${data.project_status}.`,
  };
}
