import Link from "next/link";
import { notFound } from "next/navigation";

import { removeRepository, selectRepository } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function SourcesPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<{ connection?: string; q?: string }>;
}) {
  const { projectId } = await params;
  const { connection: chosen, q } = await searchParams;
  const api = await apiClient(`/projects/${projectId}/sources`);
  const path = { params: { path: { project_id: projectId } } };
  const [project, perms, selected, scope, connections] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
    api.GET("/v1/projects/{project_id}/sources/repositories", path),
    api.GET("/v1/projects/{project_id}/sources/scope", path),
    api.GET("/v1/connections"),
  ]);
  if (!project.data || !perms.data || !selected.data || !scope.data) notFound();
  const p = project.data;
  // UI convenience only: the API enforces project.source.manage.
  const canManage = perms.data.permissions.includes("project.source.manage") && p.status !== "ARCHIVED";
  const usable = (connections.data ?? []).filter((c) => c.status === "ACTIVE" || c.status === "DEGRADED");
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
          <p className="hint">What indexing may read: selected, still-readable repositories and branches only.</p>
          <ul data-testid="sync-scope">
            {scope.data.repositories.map((s) => (
              <li key={`${s.connection_id}/${s.provider_repo_id}`}>
                {s.full_name}: {s.branches.join(", ")}
              </li>
            ))}
            {scope.data.excluded.map((x) => (
              <li key={x.full_name} className="excluded">
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
      </div>
    </main>
  );
}
