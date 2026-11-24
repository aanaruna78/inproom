import {
  checkConnection,
  createConfluenceConnection,
  createConnection,
  createJiraConnection,
  generateWebhookSecret,
  revokeConnection,
  rotateConnection,
} from "@/app/actions";
import Link from "next/link";

import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

type Issue = { code: string; message: string; repository?: string; project?: string; space?: string; severity?: string };

export default async function IntegrationsPage() {
  const api = await apiClient("/integrations");
  const [connections, me] = await Promise.all([api.GET("/v1/connections"), api.GET("/v1/me")]);
  // UI convenience only: the API enforces tenant.connection.manage.
  const isAdmin = (me.data?.roles ?? []).includes("tenant_admin");
  const items = connections.data ?? [];
  // Admin-only rollup (INPROOM-146); viewers simply don't get the panel.
  const attention = isAdmin ? ((await api.GET("/v1/connections/attention")).data ?? []) : [];

  return (
    <main>
      <h1>Integrations</h1>
      <p className="crumbs">
        Git providers, Jira and Confluence sites connected to this tenant. Projects choose their sources from these.
      </p>
      {attention.length > 0 && (
        <section className="card wide attention" aria-labelledby="attention" data-testid="needs-attention">
          <h2 id="attention">Needs attention</h2>
          <ul className="issues">
            {attention.map((a) => (
              <li key={a.connection_id} className="problem" data-testid="attention-item" aria-label={a.display_name}>
                <Link href={`/integrations/${a.connection_id}`}>
                  <strong>{a.display_name}</strong>
                </Link>{" "}
                <span className={`chip ${a.status}`}>{a.status}</span>
                {a.affected_sources > 0 && <span className="hint"> · {a.affected_sources} source(s) affected</span>}
                <ul>
                  {(a.problems as { code: string; title?: string; action?: string; source?: string; project?: string }[])
                    .slice(0, 4)
                    .map((p, n) => (
                      <li key={n}>
                        <strong>{p.title ?? p.code}</strong>
                        {p.source && <> · {p.project}/{p.source}</>} — {p.action}
                      </li>
                    ))}
                </ul>
              </li>
            ))}
          </ul>
        </section>
      )}
      <div className="grid">
        <section className="card" aria-labelledby="connections">
          <h2 id="connections">Connections</h2>
          {items.length === 0 && <p className="empty">Nothing connected yet.</p>}
          {items.map((c) => {
            const issues = ((c.health?.issues ?? []) as Issue[]) || [];
            return (
              <article key={c.id} className="connection" data-testid="connection" aria-label={c.display_name}>
                <h3>
                  <Link href={`/integrations/${c.id}`}>{c.display_name}</Link>{" "}
                  <span className={`chip ${c.status}`}>{c.status}</span>
                </h3>
                <dl className="facts">
                  <dt>Provider</dt><dd>{c.provider} · <code>{c.base_url}</code></dd>
                  <dt>Account</dt><dd>{c.account_login ?? "—"}</dd>
                  <dt>Scopes</dt>
                  <dd>
                    {c.provider === "jira"
                      ? "checked per Jira project (Browse required; write access is reported, never used)"
                      : c.provider === "confluence"
                        ? "checked per space (read required; edit access is reported, never used)"
                        : c.granted_scopes.length
                        ? c.granted_scopes.join(", ")
                        : "fine-grained (per repository)"}
                  </dd>
                  <dt>Token</dt><dd data-testid="fingerprint">{c.credential_fingerprint ?? "destroyed"}</dd>
                  <dt>Last check</dt>
                  <dd>{c.last_checked_at ? new Date(c.last_checked_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : "—"}</dd>
                </dl>
                {issues.length > 0 && (
                  <ul className="issues" data-testid="health-issues">
                    {issues.map((i, n) => (
                      <li key={n} className={i.severity === "warning" ? "warning" : "problem"}>
                        <strong>{i.code}</strong>
                        {(i.repository ?? i.project ?? i.space) && <> · {i.repository ?? i.project ?? i.space}</>} — {i.message}
                      </li>
                    ))}
                  </ul>
                )}
                {isAdmin && c.status !== "REVOKED" && (c.provider === "jira" || c.provider === "confluence") && (
                  <details className="webhook-setup" data-testid="webhook-setup">
                    <summary>Webhook setup (optional, for near-real-time updates)</summary>
                    <p className="hint">
                      Without a webhook INPROOM still picks up changes on its regular schedule. To be notified
                      immediately, add a webhook in {c.provider === "jira" ? "Jira" : "Confluence"} administration with the
                      URL and secret generated here. Generating again replaces the previous secret.
                    </p>
                    <ActionForm
                      action={generateWebhookSecret}
                      testId="webhook-secret"
                      submitLabel="Generate webhook secret"
                      hidden={{ connection_id: c.id }}
                      fields={[]}
                    />
                  </details>
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
                          hint: "Destroys the stored token and pauses every repository or Jira project using it.",
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
        {isAdmin && (
          <section className="card" aria-labelledby="connect-jira">
            <h2 id="connect-jira">Connect Jira</h2>
            <ActionForm
              action={createJiraConnection}
              testId="connect-jira"
              submitLabel="Connect Jira"
              fields={[
                { name: "display_name", label: "Name", required: true, maxLength: 80, defaultValue: "Jira" },
                {
                  name: "base_url",
                  label: "Site URL",
                  type: "url",
                  required: true,
                  placeholder: "https://your-site.atlassian.net",
                  hint: "Jira Cloud site, or your Data Center base URL. Only allowlisted hosts are accepted.",
                },
                {
                  name: "email",
                  label: "Account email",
                  maxLength: 254,
                  hint: "Jira Cloud: the Atlassian account the token belongs to. Leave empty for a Data Center personal access token.",
                },
                {
                  name: "token",
                  label: "API token",
                  type: "password",
                  required: true,
                  minLength: 20,
                  hint: "A token for an account that can browse the projects INPROOM should read. Stored encrypted; never shown again.",
                },
              ]}
            />
          </section>
        )}
        {isAdmin && (
          <section className="card" aria-labelledby="connect-confluence">
            <h2 id="connect-confluence">Connect Confluence</h2>
            <ActionForm
              action={createConfluenceConnection}
              testId="connect-confluence"
              submitLabel="Connect Confluence"
              fields={[
                { name: "display_name", label: "Name", required: true, maxLength: 80, defaultValue: "Confluence" },
                {
                  name: "base_url",
                  label: "Site URL",
                  type: "url",
                  required: true,
                  placeholder: "https://your-site.atlassian.net",
                  hint: "Confluence Cloud site (INPROOM adds /wiki), or your Data Center base URL. Only allowlisted hosts are accepted.",
                },
                {
                  name: "email",
                  label: "Account email",
                  maxLength: 254,
                  hint: "Confluence Cloud: the Atlassian account the token belongs to. Leave empty for a Data Center personal access token.",
                },
                {
                  name: "token",
                  label: "API token",
                  type: "password",
                  required: true,
                  minLength: 20,
                  hint: "A token for an account that can read the spaces INPROOM should use. Stored encrypted; never shown again.",
                },
              ]}
            />
          </section>
        )}
      </div>
    </main>
  );
}
