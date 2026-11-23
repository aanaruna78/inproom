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
