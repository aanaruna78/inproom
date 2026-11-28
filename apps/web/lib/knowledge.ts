/** Knowledge Explorer URL state and small helpers (INPROOM-228). Every selection is a URL. */

export const BROWSE_KINDS = [
  { kind: "upload", label: "Documents" },
  { kind: "confluence", label: "Confluence pages" },
  { kind: "jira", label: "Jira issues" },
  { kind: "git", label: "Code repositories" },
] as const;

export type ItemRef = { kind: string; source_id: string; external_id: string };

/** "kind|source_id|external_id" - unambiguous: kinds and UUIDs never contain "|". */
export function encodeItem(ref: ItemRef): string {
  return `${ref.kind}|${ref.source_id}|${ref.external_id}`;
}

export function decodeItem(value: string | undefined): ItemRef | null {
  if (!value) return null;
  const [kind, source_id, ...rest] = value.split("|");
  const external_id = rest.join("|");
  if (!kind || !source_id || !external_id) return null;
  return { kind, source_id, external_id };
}

export type ExplorerState = {
  kind?: string;
  cursor?: string;
  item?: string;
  q?: string;
  mode?: string;
  kinds?: string;
  days?: string;
  fresh?: string;
  node?: string;
  view?: string;
  edges?: string;
  density?: string;
};

/** A URL to the explorer with `changes` applied (undefined removes a key). */
export function explorerHref(projectId: string, state: ExplorerState, changes: ExplorerState = {}): string {
  const merged: Record<string, string | undefined> = { ...state, ...changes };
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(merged)) if (v) query.set(k, v);
  const qs = query.toString();
  return `/projects/${projectId}/knowledge${qs ? `?${qs}` : ""}`;
}

export function formatTime(value?: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-GB", { timeZone: "UTC", dateStyle: "medium", timeStyle: "short" }) + " UTC";
}

export const HEALTH_CHIP: Record<string, string> = {
  fresh: "ACTIVE",
  current: "ACTIVE",
  stale: "DEGRADED",
  failing: "INVALID",
  never: "DEGRADED",
  paused: "PAUSED",
  excluded: "PAUSED",
  superseded: "DEGRADED",
};
