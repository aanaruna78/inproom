import Link from "next/link";
import { notFound } from "next/navigation";

import { SettingsForm } from "@/components/SettingsForm";
import { apiClient, APPROVAL_SCOPES, SOURCE_KINDS, type ProjectSettings } from "@/lib/api";

export const dynamic = "force-dynamic";

const show = (v: unknown) => (Array.isArray(v) ? v.join(", ") || "none" : v === null ? "—" : String(v));

function ReadOnlySettings({ s }: { s: ProjectSettings }) {
  const rows: [string, unknown][] = [
    ["Process package", s.process_package],
    ["Write mode", s.write_mode],
    ["Model policy", s.model_policy],
    ["Required approvals", s.approvals?.required_scopes],
    ["Minimum approvers", s.approvals?.min_approvers],
    ["Approval expiry (hours)", s.approvals?.expiry_hours],
    ["Allowed sources", s.sources?.allowed_kinds],
    ["Highest classification", s.sources?.max_classification],
    ["New sources need approval", s.sources?.approve_new_sources ? "yes" : "no"],
    ["Production actions", s.production?.actions_enabled ? `on (${show(s.production.environments)})` : "off"],
  ];
  return (
    <dl className="facts" data-testid="settings-read-only">
      {rows.map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{show(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

export default async function SettingsPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<{ version?: string }>;
}) {
  const { projectId } = await params;
  const { version: selectedRaw } = await searchParams;
  const api = await apiClient(`/projects/${projectId}/settings`);
  const path = { params: { path: { project_id: projectId } } };
  const [project, perms, current, history, users] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
    api.GET("/v1/projects/{project_id}/settings", path),
    api.GET("/v1/projects/{project_id}/settings/versions", { params: { ...path.params, query: { limit: 50 } } }),
    api.GET("/v1/users", { params: { query: { limit: 100 } } }),
  ]);
  // Show people by name (tenant directory) rather than by identity-provider id.
  const names = new Map((users.data?.items ?? []).map((u) => [u.user_id, u.display_name ?? u.email ?? u.user_id]));
  const who = (id: string | null | undefined) => (id ? (names.get(id) ?? id) : "—");
  if (!project.data || !perms.data || !current.data || !history.data) notFound();
  const p = project.data;
  const s = current.data;
  const selected = Number(selectedRaw) || null;
  const detail = selected
    ? await api.GET("/v1/projects/{project_id}/settings/versions/{version}", {
        params: { path: { project_id: projectId, version: selected } },
      })
    : null;
  const archived = p.status === "ARCHIVED";
  // UI convenience only; the API enforces project.settings.update and the production rule.
  const canEdit = perms.data.permissions.includes("project.settings.update") && !archived;
  const canManageProduction = perms.data.tenant_roles.includes("tenant_admin");

  return (
    <main>
      <h1>Settings for {p.name}</h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> /{" "}
        <Link href={`/workspaces/${p.workspace_id}/projects`}>Projects</Link> /{" "}
        <Link href={`/projects/${p.id}`}>{p.key}</Link> / Settings
      </p>
      <div className="grid">
        <section className="card" aria-labelledby="current">
          <h2 id="current">
            Version {s.version} <span className="hint">by {who(s.created_by)}</span>
          </h2>
          {archived && (
            <p className="alert error" data-testid="settings-archived">
              This project is archived, so its settings are read-only. Restore it from the project page to edit.
            </p>
          )}
          {canEdit ? (
            <SettingsForm
              projectId={p.id}
              version={s.version}
              settings={s.settings as never}
              approvalScopes={APPROVAL_SCOPES}
              sourceKinds={SOURCE_KINDS}
              canManageProduction={canManageProduction}
            />
          ) : (
            <ReadOnlySettings s={s.settings} />
          )}
          {s.settings.production?.actions_enabled && s.settings.production.enabled_by && (
            <p className="hint" data-testid="production-enabled-by">
              Production actions enabled by {who(s.settings.production.enabled_by)} on{" "}
              {new Date(s.settings.production.enabled_at ?? "").toLocaleString("en-GB", { timeZone: "UTC" })} UTC
            </p>
          )}
        </section>
        <section className="card" aria-labelledby="history">
          <h2 id="history">History</h2>
          <table data-testid="settings-history">
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Changed</th>
              </tr>
            </thead>
            <tbody>
              {history.data.items.map((v) => (
                <tr key={v.version} data-testid={`settings-version-${v.version}`} aria-current={v.version === selected || undefined}>
                  <td>
                    <Link href={`/projects/${p.id}/settings?version=${v.version}`}>v{v.version}</Link>
                    <div className="hint">
                      {who(v.created_by)}, {new Date(v.created_at).toLocaleString("en-GB", { timeZone: "UTC" })} UTC
                    </div>
                  </td>
                  <td>
                    {v.changed_paths.length ? v.changed_paths.join(", ") : v.version === 1 ? "initial settings" : "—"}
                    {v.change_note && <div className="hint">“{v.change_note}”</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {detail?.data && (
            <div data-testid="settings-diff">
              <h3>Changes in v{detail.data.version}</h3>
              {detail.data.diff.length === 0 ? (
                <p className="empty">Initial settings - nothing to compare.</p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Setting</th>
                      <th scope="col">Before</th>
                      <th scope="col">After</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.diff.map((c) => (
                      <tr key={c.path}>
                        <td><code>{c.path}</code></td>
                        <td>{show(c.old)}</td>
                        <td>{show(c.new)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
