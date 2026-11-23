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
