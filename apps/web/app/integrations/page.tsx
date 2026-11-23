import { checkConnection, createConnection, revokeConnection, rotateConnection } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

type Issue = { code: string; message: string; repository?: string; severity?: string };

export default async function IntegrationsPage() {
  const api = await apiClient("/integrations");
  const [connections, me] = await Promise.all([api.GET("/v1/connections"), api.GET("/v1/me")]);
  // UI convenience only: the API enforces tenant.connection.manage.
  const isAdmin = (me.data?.roles ?? []).includes("tenant_admin");
  const items = connections.data ?? [];

  return (
    <main>
      <h1>Integrations</h1>
      <p className="crumbs">Git providers connected to this tenant. Projects choose repositories from these.</p>
      <div className="grid">
        <section className="card" aria-labelledby="connections">
          <h2 id="connections">Connections</h2>
          {items.length === 0 && <p className="empty">No Git provider connected yet.</p>}
          {items.map((c) => {
            const issues = ((c.health?.issues ?? []) as Issue[]) || [];
            return (
              <article key={c.id} className="connection" data-testid="connection" aria-label={c.display_name}>
                <h3>
                  {c.display_name} <span className={`chip ${c.status}`}>{c.status}</span>
                </h3>
                <dl className="facts">
                  <dt>Provider</dt><dd>{c.provider} · <code>{c.base_url}</code></dd>
                  <dt>Account</dt><dd>{c.account_login ?? "—"}</dd>
                  <dt>Scopes</dt><dd>{c.granted_scopes.length ? c.granted_scopes.join(", ") : "fine-grained (per repository)"}</dd>
                  <dt>Token</dt><dd data-testid="fingerprint">{c.credential_fingerprint ?? "destroyed"}</dd>
                  <dt>Last check</dt>
                  <dd>{c.last_checked_at ? new Date(c.last_checked_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : "—"}</dd>
                </dl>
                {issues.length > 0 && (
                  <ul className="issues" data-testid="health-issues">
                    {issues.map((i, n) => (
                      <li key={n} className={i.severity === "warning" ? "warning" : "problem"}>
                        <strong>{i.code}</strong>
                        {i.repository && <> · {i.repository}</>} — {i.message}
                      </li>
                    ))}
                  </ul>
                )}
                {isAdmin && c.status !== "REVOKED" && (
                  <div className="actions">
                    <ActionForm action={checkConnection} testId="check" submitLabel="Check now" hidden={{ connection_id: c.id }} fields={[]} />
                    <ActionForm
                      action={rotateConnection}
                      testId="rotate"
                      submitLabel="Rotate token"
                      hidden={{ connection_id: c.id }}
                      fields={[{ name: "token", label: "New token", type: "password", required: true, minLength: 20 }]}
                    />
                    <ActionForm
                      action={revokeConnection}
                      testId="revoke"
                      submitLabel="Revoke connection"
                      hidden={{ connection_id: c.id }}
                      fields={[
                        {
                          name: "confirm",
                          label: "Type REVOKE to confirm",
                          hint: "Destroys the stored token and pauses every repository using it.",
                          required: true,
                        },
                      ]}
                    />
                  </div>
                )}
              </article>
            );
          })}
        </section>
        <section className="card" aria-labelledby="connect">
          <h2 id="connect">Connect GitHub</h2>
          {isAdmin ? (
            <ActionForm
              action={createConnection}
              testId="connect"
              submitLabel="Connect"
              fields={[
                { name: "display_name", label: "Name", required: true, maxLength: 80, defaultValue: "GitHub" },
                {
                  name: "base_url",
                  label: "API URL",
                  type: "url",
                  defaultValue: "https://api.github.com",
                  hint: "GitHub Enterprise: https://<host>/api/v3. Only allowlisted hosts are accepted.",
                },
                {
                  name: "token",
                  label: "Fine-grained token",
                  type: "password",
                  required: true,
                  minLength: 20,
                  hint: "Read-only Contents + Metadata on the repositories INPROOM may read (add Webhooks to register webhooks). Stored encrypted; never shown again.",
                },
              ]}
            />
          ) : (
            <p className="empty" data-testid="integrations-read-only">
              Only tenant admins can connect or change Git providers.
            </p>
          )}
        </section>
      </div>
    </main>
  );
}
