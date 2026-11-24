import Link from "next/link";
import { notFound } from "next/navigation";

import {
  reconcileSource,
  setSourceExcluded,
  removeConfluenceSpace,
  removeJiraProject,
  removeRepository,
  selectConfluenceSpace,
  selectJiraProject,
  selectRepository,
  syncConfluenceSpace,
} from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

type Filters = { root_page_ids?: string[]; required_labels?: string[]; include_restricted?: boolean };

function describeFilters(f: Filters): string {
  const parts = [];
  if (f.root_page_ids?.length) parts.push(`trees ${f.root_page_ids.join(", ")}`);
  if (f.required_labels?.length) parts.push(`labels ${f.required_labels.join(", ")}`);
  if (f.include_restricted) parts.push("restricted included");
  return parts.length ? parts.join(" · ") : "whole space";
}

export default async function SourcesPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<{
    connection?: string;
    q?: string;
    jira_connection?: string;
    jq?: string;
    confluence_connection?: string;
    cq?: string;
  }>;
}) {
  const { projectId } = await params;
  const { connection: chosen, q, jira_connection: jiraChosen, jq, confluence_connection: confChosen, cq } = await searchParams;
  const api = await apiClient(`/projects/${projectId}/sources`);
  const path = { params: { path: { project_id: projectId } } };
  const [project, perms, selected, scope, connections, jiraSources, confSources, syncStatus] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
    api.GET("/v1/projects/{project_id}/sources/repositories", path),
    api.GET("/v1/projects/{project_id}/sources/scope", path),
    api.GET("/v1/connections"),
    api.GET("/v1/projects/{project_id}/sources/jira", path),
    api.GET("/v1/projects/{project_id}/sources/confluence", path),
    api.GET("/v1/projects/{project_id}/sources/sync-status", path),
  ]);
  if (!project.data || !perms.data || !selected.data || !scope.data) notFound();
  const p = project.data;
  // UI convenience only: the API enforces project.source.manage.
  const canManage = perms.data.permissions.includes("project.source.manage") && p.status !== "ARCHIVED";
  const active = (connections.data ?? []).filter((c) => c.status === "ACTIVE" || c.status === "DEGRADED");
  const usable = active.filter((c) => c.provider === "github");
  const jiraUsable = active.filter((c) => c.provider === "jira");
  const jiraConnectionId = jiraChosen ?? jiraUsable[0]?.id;
  const jiraOptions =
    canManage && jiraConnectionId
      ? await api.GET("/v1/connections/{connection_id}/jira/projects", {
          params: { path: { connection_id: jiraConnectionId }, query: { project_id: projectId, q: jq || undefined } },
        })
      : null;
  const jira = jiraSources.data ?? [];
  const confUsable = active.filter((c) => c.provider === "confluence");
  const confConnectionId = confChosen ?? confUsable[0]?.id;
  const spaceOptions =
    canManage && confConnectionId
      ? await api.GET("/v1/connections/{connection_id}/confluence/spaces", {
          params: { path: { connection_id: confConnectionId }, query: { project_id: projectId, q: cq || undefined } },
        })
      : null;
  const spaces = confSources.data ?? [];
  const chosenFilters = new Map(spaces.map((sp) => [sp.space_id, sp.filters as Filters]));
  const connectionId = chosen ?? usable[0]?.id;
  const options =
    canManage && connectionId
      ? await api.GET("/v1/connections/{connection_id}/repositories", {
          params: { path: { connection_id: connectionId }, query: { project_id: projectId, q: q || undefined } },
        })
      : null;

  return (
    <main>
      <h1>Sources for {p.name}</h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> /{" "}
        <Link href={`/workspaces/${p.workspace_id}/projects`}>Projects</Link> /{" "}
        <Link href={`/projects/${p.id}`}>{p.key}</Link> / Sources
      </p>
      <section className="card wide" aria-labelledby="sync-status">
        <h2 id="sync-status">Sync status</h2>
        <p className="hint">
          A background worker re-checks every source about every{" "}
          {Math.round((syncStatus.data?.interval_seconds ?? 900) / 60)} minutes, and immediately when a webhook arrives.
          Stale = no successful check for 3 intervals.
        </p>
        {(syncStatus.data?.sources ?? []).length === 0 ? (
          <p className="empty">No sources selected yet.</p>
        ) : (
          <table data-testid="sync-status">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Health</th>
                <th scope="col">Data as of</th>
                <th scope="col">Next check</th>
                <th scope="col"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {(syncStatus.data?.sources ?? []).map((st) => (
                <tr key={st.source_id} data-testid="sync-row" aria-label={`${st.kind} ${st.name}`}>
                  <td>
                    <strong>{st.name}</strong> <span className="hint">{st.kind}</span>
                  </td>
                  <td>
                    <span
                      className={`chip ${({ fresh: "ACTIVE", failing: "INVALID", paused: "PAUSED", excluded: "PAUSED" } as Record<string, string>)[st.health] ?? "DEGRADED"}`}
                      data-testid="sync-health"
                    >
                      {st.health}
                    </span>
                    {st.pause_cause === "excluded" && <div className="hint">{st.status_reason}</div>}
                    {st.health === "paused" && st.status_reason && <div className="hint">{st.status_reason}</div>}
                    {st.running && <span className="chip"> running</span>}
                    {st.queued && <span className="chip"> queued</span>}
                    {st.last_error && st.consecutive_failures > 0 && (
                      <div className="hint warning" data-testid="sync-error">
                        {(st.last_error as { title?: string; code?: string }).title ?? (st.last_error as { code?: string }).code}{" "}
                        ({st.consecutive_failures} failed attempt{st.consecutive_failures === 1 ? "" : "s"}) —{" "}
                        {(st.last_error as { action?: string }).action}
                      </div>
                    )}
                  </td>
                  <td data-testid="sync-last-success">
                    {st.last_success_at
                      ? new Date(st.last_success_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC"
                      : "never"}
                  </td>
                  <td>
                    {st.next_due_at ? new Date(st.next_due_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : "—"}
                  </td>
                  <td>
                    {canManage && st.source_status === "ACTIVE" && (
                      <ActionForm
                        action={reconcileSource}
                        testId="reconcile"
                        submitLabel="Reconcile now"
                        hidden={{ project_id: p.id, kind: st.kind, source_id: st.source_id }}
                        fields={[]}
                      />
                    )}
                    {canManage && (
                      <ActionForm
                        action={setSourceExcluded}
                        testId={st.pause_cause === "excluded" ? "include" : "exclude"}
                        submitLabel={st.pause_cause === "excluded" ? "Include again" : "Exclude"}
                        hidden={{
                          project_id: p.id,
                          kind: st.kind,
                          source_id: st.source_id,
                          action: st.pause_cause === "excluded" ? "include" : "exclude",
                        }}
                        fields={[]}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <div className="grid">
        <section className="card" aria-labelledby="selected">
          <h2 id="selected">Selected repositories</h2>
          {selected.data.length === 0 ? (
            <p className="empty">No repositories selected. INPROOM reads nothing from Git for this project.</p>
          ) : (
            <table data-testid="selected-repositories">
              <thead>
                <tr>
                  <th scope="col">Repository</th>
                  <th scope="col">Branches</th>
                  <th scope="col">Status</th>
                  <th scope="col"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {selected.data.map((r) => {
                  const excess = ["admin", "maintain", "push"].filter((k) => r.permissions[k]);
                  return (
                    <tr key={r.id} data-testid="selected-repository">
                      <td>
                        <strong>{r.full_name}</strong> {r.private && <span className="chip">private</span>}
                        <div className="hint">via {r.connection_name}</div>
                        {excess.length > 0 && (
                          <div className="hint warning">Token has more than read access ({excess.join(", ")})</div>
                        )}
                      </td>
                      <td>{r.branches.map((b) => <code key={b} className="branch">{b}</code>)}</td>
                      <td>
                        <span className={`chip ${r.status}`}>{r.status}</span>
                        {r.status_reason && <div className="hint">{r.status_reason}</div>}
                        <div className="hint">webhook: {r.webhook_status.replace("_", " ")}</div>
                      </td>
                      <td>
                        {canManage && (
                          <ActionForm
                            action={removeRepository}
                            testId="remove-repository"
                            submitLabel="Remove"
                            hidden={{ project_id: p.id, connection_id: r.connection_id, repo_id: r.provider_repo_id }}
                            fields={[]}
                          />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <h3>Sync scope</h3>
          <p className="hint">
            What indexing may read: selected, still-readable repositories, branches and Jira projects only.
          </p>
          <ul data-testid="sync-scope">
            {scope.data.repositories.map((s) => (
              <li key={`${s.connection_id}/${s.provider_repo_id}`}>
                {s.full_name}: {s.branches.join(", ")}
              </li>
            ))}
            {(scope.data.confluence_spaces ?? []).map((c) => (
              <li key={c.source_id}>
                Confluence {c.space_key}: {c.page_count} pages ({describeFilters(c.filters as Filters)})
              </li>
            ))}
            {(scope.data.jira_projects ?? []).map((j) => (
              <li key={j.source_id}>
                Jira {j.jira_key} (schema v{j.snapshot_version}, mapping v{j.mapping_version})
              </li>
            ))}
            {scope.data.excluded.map((x) => (
              <li key={`${x.kind}:${x.full_name}`} className="excluded">
                {x.full_name} — excluded ({x.reason})
              </li>
            ))}
          </ul>
        </section>
        <section className="card" aria-labelledby="add">
          <h2 id="add">Add repositories</h2>
          {!canManage ? (
            <p className="empty" data-testid="sources-read-only">Only project admins can change this project&apos;s sources.</p>
          ) : usable.length === 0 ? (
            <p className="empty">
              No active Git connection. A tenant admin can connect one under <Link href="/integrations">Integrations</Link>.
            </p>
          ) : (
            <>
              <form className="stack" method="get">
                <label>
                  Connection
                  <select name="connection" defaultValue={connectionId}>
                    {usable.map((c) => (
                      <option key={c.id} value={c.id}>{c.display_name} ({c.account_login})</option>
                    ))}
                  </select>
                </label>
                <label>
                  Search repositories
                  <input name="q" defaultValue={q ?? ""} placeholder="name contains…" />
                </label>
                <button type="submit">Show repositories</button>
              </form>
              {options?.error && <p className="alert error">{(options.error as { detail?: string }).detail}</p>}
              <ul className="options" data-testid="repository-options">
                {(options?.data?.items ?? []).map((o) => (
                  <li key={o.id} data-testid="repository-option" aria-label={o.full_name}>
                    <strong>{o.full_name}</strong> {o.private && <span className="chip">private</span>}{" "}
                    {o.selected && <span className="chip ACTIVE">selected</span>}
                    {o.excess_privileges.length > 0 && (
                      <div className="hint warning">Token can {o.excess_privileges.join(", ")} — INPROOM only needs read</div>
                    )}
                    <ActionForm
                      action={selectRepository}
                      testId={`select-${o.full_name.replace("/", "-")}`}
                      submitLabel={o.selected ? "Update branches" : "Select"}
                      hidden={{ project_id: p.id, connection_id: connectionId!, repo_id: o.id }}
                      fields={[
                        { name: "branches", label: "Branches (comma-separated)", defaultValue: o.default_branch },
                        { name: "register_webhook", label: "Register a webhook for change notifications", type: "checkbox" },
                      ]}
                    />
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
        <section className="card" aria-labelledby="jira-selected">
          <h2 id="jira-selected">Jira projects</h2>
          {jira.length === 0 ? (
            <p className="empty">No Jira projects selected. INPROOM reads nothing from Jira for this project.</p>
          ) : (
            <table data-testid="selected-jira-projects">
              <thead>
                <tr>
                  <th scope="col">Jira project</th>
                  <th scope="col">Status</th>
                  <th scope="col">Access</th>
                  <th scope="col"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {jira.map((j) => {
                  const warnings = j.warnings as { code: string; message: string }[];
                  return (
                    <tr key={j.id} data-testid="selected-jira-project" aria-label={j.jira_key}>
                      <td>
                        <Link href={`/projects/${p.id}/sources/jira/${j.id}`}>
                          <strong>{j.jira_key}</strong> · {j.name}
                        </Link>
                        <div className="hint">via {j.connection_name} · schema v{j.snapshot_version}</div>
                        {warnings.length > 0 && (
                          <ul className="issues" data-testid="jira-warnings">
                            {warnings.map((w) => (
                              <li key={w.code} className="warning">
                                <strong>{w.code}</strong> — {w.message}
                              </li>
                            ))}
                          </ul>
                        )}
                      </td>
                      <td>
                        <span className={`chip ${j.status}`}>{j.status}</span>
                        {j.status_reason && <div className="hint">{j.status_reason}</div>}
                      </td>
                      <td data-testid="jira-access">
                        {(j.capabilities as { write_capable?: boolean }).write_capable ? "read + write" : "read only"}
                      </td>
                      <td>
                        {canManage && (
                          <ActionForm
                            action={removeJiraProject}
                            testId="remove-jira-project"
                            submitLabel="Remove"
                            hidden={{ project_id: p.id, connection_id: j.connection_id, jira_key: j.jira_key }}
                            fields={[]}
                          />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>
        <section className="card" aria-labelledby="jira-add">
          <h2 id="jira-add">Add Jira projects</h2>
          {!canManage ? (
            <p className="empty" data-testid="jira-read-only">Only project admins can change this project&apos;s sources.</p>
          ) : jiraUsable.length === 0 ? (
            <p className="empty">
              No active Jira connection. A tenant admin can connect one under <Link href="/integrations">Integrations</Link>.
            </p>
          ) : (
            <>
              <form className="stack" method="get">
                <label>
                  Jira connection
                  <select name="jira_connection" defaultValue={jiraConnectionId}>
                    {jiraUsable.map((c) => (
                      <option key={c.id} value={c.id}>{c.display_name} ({c.base_url})</option>
                    ))}
                  </select>
                </label>
                <label>
                  Search Jira projects
                  <input name="jq" defaultValue={jq ?? ""} placeholder="key or name contains…" />
                </label>
                <button type="submit">Show Jira projects</button>
              </form>
              {jiraOptions?.error && <p className="alert error">{(jiraOptions.error as { detail?: string }).detail}</p>}
              <ul className="options" data-testid="jira-options">
                {(jiraOptions?.data?.items ?? []).map((o) => (
                  <li key={o.id} data-testid="jira-option" aria-label={o.key}>
                    <strong>{o.key}</strong> · {o.name} {o.selected && <span className="chip ACTIVE">selected</span>}
                    {!o.selected && (
                      <ActionForm
                        action={selectJiraProject}
                        testId={`select-jira-${o.key}`}
                        submitLabel="Select"
                        hidden={{ project_id: p.id, connection_id: jiraConnectionId!, jira_key: o.key }}
                        fields={[]}
                      />
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
        <section className="card" aria-labelledby="confluence-selected">
          <h2 id="confluence-selected">Confluence spaces</h2>
          {spaces.length === 0 ? (
            <p className="empty">No Confluence spaces selected. INPROOM reads nothing from Confluence for this project.</p>
          ) : (
            <table data-testid="selected-spaces">
              <thead>
                <tr>
                  <th scope="col">Space</th>
                  <th scope="col">Pages</th>
                  <th scope="col">Status</th>
                  <th scope="col"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {spaces.map((sp) => {
                  const warnings = sp.warnings as { code: string; message: string }[];
                  const last = sp.last_sync as { truncated?: boolean };
                  return (
                    <tr key={sp.id} data-testid="selected-space" aria-label={sp.space_key}>
                      <td>
                        <Link href={`/projects/${p.id}/sources/confluence/${sp.id}`}>
                          <strong>{sp.space_key}</strong> · {sp.name}
                        </Link>
                        <div className="hint">
                          via {sp.connection_name} · {describeFilters(sp.filters as Filters)}
                        </div>
                        {warnings.length > 0 && (
                          <ul className="issues" data-testid="space-warnings">
                            {warnings.map((w) => (
                              <li key={w.code} className="warning">
                                <strong>{w.code}</strong> — {w.message}
                              </li>
                            ))}
                          </ul>
                        )}
                      </td>
                      <td data-testid="space-counts">
                        {sp.page_counts.CURRENT} in scope
                        <div className="hint">
                          {sp.page_counts.RESTRICTED} restricted · {sp.page_counts.EXCLUDED} excluded ·{" "}
                          {sp.page_counts.DELETED} deleted{last.truncated ? " · truncated" : ""}
                        </div>
                      </td>
                      <td>
                        <span className={`chip ${sp.status}`}>{sp.status}</span>
                        {sp.status_reason && <div className="hint">{sp.status_reason}</div>}
                        <div className="hint">
                          last sync{" "}
                          {sp.last_synced_at
                            ? new Date(sp.last_synced_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC"
                            : "never"}
                        </div>
                      </td>
                      <td>
                        {canManage && sp.status === "ACTIVE" && (
                          <ActionForm
                            action={syncConfluenceSpace}
                            testId="sync-space"
                            submitLabel="Sync now"
                            hidden={{ project_id: p.id, source_id: sp.id }}
                            fields={[]}
                          />
                        )}
                        {canManage && (
                          <ActionForm
                            action={removeConfluenceSpace}
                            testId="remove-space"
                            submitLabel="Remove"
                            hidden={{ project_id: p.id, connection_id: sp.connection_id, space_key: sp.space_key }}
                            fields={[]}
                          />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>
        <section className="card" aria-labelledby="confluence-add">
          <h2 id="confluence-add">Add Confluence spaces</h2>
          {!canManage ? (
            <p className="empty" data-testid="confluence-read-only">Only project admins can change this project&apos;s sources.</p>
          ) : confUsable.length === 0 ? (
            <p className="empty">
              No active Confluence connection. A tenant admin can connect one under <Link href="/integrations">Integrations</Link>.
            </p>
          ) : (
            <>
              <form className="stack" method="get">
                <label>
                  Confluence connection
                  <select name="confluence_connection" defaultValue={confConnectionId}>
                    {confUsable.map((c) => (
                      <option key={c.id} value={c.id}>{c.display_name} ({c.base_url})</option>
                    ))}
                  </select>
                </label>
                <label>
                  Search spaces
                  <input name="cq" defaultValue={cq ?? ""} placeholder="key or name contains…" />
                </label>
                <button type="submit">Show spaces</button>
              </form>
              {spaceOptions?.error && <p className="alert error">{(spaceOptions.error as { detail?: string }).detail}</p>}
              <ul className="options" data-testid="space-options">
                {(spaceOptions?.data?.items ?? []).map((o) => {
                  const current = chosenFilters.get(o.id) ?? {};
                  return (
                    <li key={o.id} data-testid="space-option" aria-label={o.key}>
                      <strong>{o.key}</strong> · {o.name} {o.selected && <span className="chip ACTIVE">selected</span>}
                      <ActionForm
                        action={selectConfluenceSpace}
                        testId={`select-space-${o.key}`}
                        submitLabel={o.selected ? "Update filters and sync" : "Select and sync"}
                        hidden={{ project_id: p.id, connection_id: confConnectionId!, space_key: o.key }}
                        fields={[
                          {
                            name: "required_labels",
                            label: "Required labels (comma-separated)",
                            defaultValue: (current.required_labels ?? []).join(", "),
                            hint: "Only pages carrying every label, e.g. approved. Empty = all pages.",
                          },
                          {
                            name: "root_page_ids",
                            label: "Page trees (root page ids, comma-separated)",
                            defaultValue: (current.root_page_ids ?? []).join(", "),
                          },
                          {
                            name: "include_restricted",
                            label: "Include pages with read restrictions (flagged)",
                            type: "checkbox",
                            defaultValue: current.include_restricted ? "on" : "",
                          },
                        ]}
                      />
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </section>
      </div>
    </main>
  );
}
