import "server-only";

import createClient from "openapi-fetch";

import type { components, paths } from "./api-schema";
import { requireAccessToken } from "./session";

export type Workspace = components["schemas"]["Workspace"];
export type Project = components["schemas"]["Project"];
export type ProjectDetail = components["schemas"]["ProjectDetail"];
export type Member = components["schemas"]["Member"];
export type MyPermissions = components["schemas"]["MyPermissions"];

export const PROJECT_ROLES = [
  "project_admin",
  "product_owner",
  "tech_lead",
  "developer",
  "reviewer",
  "qa_lead",
  "tester",
  "release_manager",
  "viewer",
] as const;
export type ProjectRole = (typeof PROJECT_ROLES)[number];

/** RFC 7807 problem returned by the INPROOM API. */
export type Problem = {
  status: number;
  code: string;
  title: string;
  detail?: string;
  trace_id?: string;
  required_permission?: string;
};

export const API_URL = process.env.INPROOM_API_URL ?? "http://localhost:8000";

/** Typed API client authenticated as the signed-in user. */
export async function apiClient(returnTo: string) {
  const token = await requireAccessToken(returnTo);
  return createClient<paths>({
    baseUrl: API_URL,
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
}

export function asProblem(error: unknown, status: number): Problem {
  if (error && typeof error === "object" && "code" in error) return error as Problem;
  if (status === 422 && error && typeof error === "object" && "errors" in error) {
    return { status, code: "VALIDATION_ERROR", title: "Invalid input", detail: "Check the highlighted fields." };
  }
  return { status, code: `HTTP_${status}`, title: "Request failed", detail: "The API returned an unexpected error." };
}
