import Link from "next/link";
import { notFound } from "next/navigation";

import { adoptSources, checkConnection, revokeConnection, rotateConnection } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

type Problem = { code: string; title?: string; explanation?: string; action?: string };
type Issue = { code: string; message: string; repository?: string; project?: string; space?: string; severity?: string };

const FRESH_CHIP: Record<string, string> = {
  fresh: "ACTIVE",
  stale: "DEGRADED",
  never: "DEGRADED",
  failing: "INVALID",
  paused: "PAUSED",
  excluded: "PAUSED",
};

export default async function ConnectionDetailPage({ params }: { params: Promise<{ connectionId: string }> }) {
  const { connectionId } = await params;
  const api = await apiClient(`/integrations/${connectionId}`);
  const path = { params: { path: { connection_id: connectionId } } };
  const [connection, me, all] = await Promise.all([
    api.GET("/v1/connections/{connection_id}", path),
    api.GET("/v1/me"),
    api.GET("/v1/connections"),
  ]);
  if (!connection.data) notFound();
  const c = connection.data;
  const isAdmin = (me.data?.roles ?? []).includes("tenant_admin");
  const [sources, impact] = isAdmin
    ? await Promise.all([
        api.GET("/v1/connections/{connection_id}/sources", path),
        c.status !== "REVOKED" ? api.GET("/v1/connections/{connection_id}/impact", path) : Promise.resolve(null),
      ])
    : [null, null];
  const issues = ((c.health?.issues ?? []) as Issue[]) || [];
  const adopters = (all.data ?? []).filter(
    (o) => o.id !== c.id && o.provider === c.provider && o.base_url === c.base_url && ["ACTIVE", "DEGRADED"].includes(o.status),
  );
  const revokedSources = (sources?.data ?? []).filter((s) => s.pause_cause === "connection_revoked").length;

  return (
    <main>
      <h1>
        {c.display_name} <span className={`chip ${c.status}`}>{c.status}</span>
      </h1>
      <p className="crumbs">
        <Link href="/integrations">Integrations</Link> / {c.display_name}
      </p>
      <div className="grid">
        <section className="card" aria-labelledby="health">
          <h2 id="health">Health</h2>
          <dl className="facts">
            <dt>Provider</dt><dd>{c.provider} · <code>{c.base_url}</code></dd>
            <dt>Account</dt><dd>{c.account_login ?? "—"}</dd>
            <dt>Scopes</dt><dd>{c.granted_scopes.length ? c.granted_scopes.join(", ") : "checked per source"}</dd>
            <dt>Token</dt><dd data-testid="fingerprint">{c.credential_fingerprint ?? "destroyed"}</dd>
            <dt>Last check</dt>
            <dd>{c.last_checked_at ? new Date(c.last_checked_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : "—"}</dd>
          </dl>
          {issues.length === 0 ? (
            <p className="hint">No problems found at the last check.</p>
          ) : (
            <ul className="issues" data-testid="health-issues">
              {issues.map((i, n) => (
                <li key={n} className={i.severity === "warning" ? "warning" : "problem"}>
                  <strong>{i.code}</strong>
                  {(i.repository ?? i.project ?? i.space) && <> · {i.repository ?? i.project ?? i.space}</>} — {i.message}
                </li>
              ))}
            </ul>
          )}
          {isAdmin && c.status !== "REVOKED" && (
            <ActionForm action={checkConnection} testId="check" submitLabel="Check now" hidden={{ connection_id: c.id }} fields={[]} />
          )}
        </section>

        {isAdmin && c.status !== "REVOKED" && (
          <section className="card" aria-labelledby="reauthorize">
            <h2 id="reauthorize">{c.status === "INVALID" ? "Reauthorize (needed)" : "Reauthorize / rotate token"}</h2>
            <p className="hint">
              The new token is validated first. INPROOM then checks every source and resumes the ones that were paused
              only because of access, and syncs them. Sources a project admin excluded stay excluded.
            </p>
            <ActionForm
              action={rotateConnection}
              testId="reauthorize"
              submitLabel="Reauthorize"
              hidden={{ connection_id: c.id }}
              fields={[{ name: "token", label: "New token", type: "password", required: true, minLength: 20 }]}
            />
          </section>
        )}

        {isAdmin && (
          <section className="card wide" aria-labelledby="sources">
            <h2 id="sources">Sources using this connection</h2>
            {(sources?.data ?? []).length === 0 ? (
              <p className="empty">No project uses this connection.</p>
            ) : (
              <table data-testid="connection-sources">
                <thead>
                  <tr>
                    <th scope="col">Project</th>
                    <th scope="col">Source</th>
                    <th scope="col">Freshness</th>
                    <th scope="col">Data as of</th>
                    <th scope="col">Problem</th>
                  </tr>
                </thead>
                <tbody>
                  {(sources?.data ?? []).map((s) => {
                    const p = s.problem as Problem | null;
                    return (
                      <tr key={s.source_id} data-testid="connection-source" aria-label={`${s.project_key} ${s.name}`}>
                        <td>
                          <Link href={`/projects/${s.project_id}/sources`}>{s.project_key}</Link>
                        </td>
                        <td>{s.name} <span className="hint">{s.kind}</span></td>
                        <td><span className={`chip ${FRESH_CHIP[s.freshness] ?? ""}`} data-testid="source-freshness">{s.freshness}</span></td>
                        <td>{s.data_as_of ? new Date(s.data_as_of).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : "never"}</td>
                        <td>{p ? <><strong>{p.title}</strong> — {p.action}</> : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
        )}

        {isAdmin && impact?.data && (
          <section className="card" aria-labelledby="disconnect">
            <h2 id="disconnect">Disconnect</h2>
            <p data-testid="impact-summary">{impact.data.summary}</p>
            <ActionForm
              action={revokeConnection}
              testId="revoke"
              submitLabel="Disconnect"
              hidden={{ connection_id: c.id }}
              fields={[{ name: "confirm", label: "Type REVOKE to confirm", required: true }]}
            />
          </section>
        )}

        {isAdmin && c.status === "REVOKED" && revokedSources > 0 && (
          <section className="card" aria-labelledby="adopt">
            <h2 id="adopt">Reconnect: move sources to a new connection</h2>
            <p className="hint">
              {revokedSources} source(s) stopped when this connection was disconnected. A new connection to the same
              site can take them over; INPROOM verifies each one with the new token before resuming it.
            </p>
            {adopters.length === 0 ? (
              <p className="empty">
                Create a new {c.provider} connection to <code>{c.base_url}</code> on <Link href="/integrations">Integrations</Link> first.
              </p>
            ) : (
              <ActionForm
                action={adoptSources}
                testId="adopt"
                submitLabel="Move sources"
                hidden={{ from_connection_id: c.id }}
                fields={[
                  {
                    name: "target_connection_id",
                    label: "New connection",
                    options: adopters.map((o) => ({ value: o.id, label: `${o.display_name} (${o.account_login})` })),
                  },
                ]}
              />
            )}
          </section>
        )}
      </div>
    </main>
  );
}
