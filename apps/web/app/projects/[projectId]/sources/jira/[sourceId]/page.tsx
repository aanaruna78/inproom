import Link from "next/link";
import { notFound } from "next/navigation";

import { rediscoverJira, saveJiraMapping } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

type Named = { id: string; name: string };
type IssueType = Named & { canonical: string; hierarchy_level: number };
type Status = Named & { category: string; canonical: string };
type JiraField = Named & { custom: boolean; schema_type: string | null; custom_type: string | null };
type Schema = {
  project: { key: string; name: string };
  issue_types: IssueType[];
  statuses: Status[];
  fields: JiraField[];
  link_types: (Named & { inward: string; outward: string })[];
  boards: (Named & { type: string })[];
  sprints: (Named & { board_id: string })[];
};
type FieldMapping = { field_id: string; source: "auto" | "override" } | null;
type Mapping = {
  fields: Record<string, FieldMapping>;
  issue_types: Record<string, string>;
  statuses: Record<string, string>;
  overrides?: { fields?: Record<string, string | null>; issue_types?: Record<string, string>; statuses?: Record<string, string> };
};

const CANONICAL_FIELDS: { name: string; label: string }[] = [
  { name: "story_points", label: "Story points" },
  { name: "sprint", label: "Sprint" },
  { name: "epic_link", label: "Epic link (legacy)" },
  { name: "acceptance_criteria", label: "Acceptance criteria" },
];

export default async function JiraSourcePage({ params }: { params: Promise<{ projectId: string; sourceId: string }> }) {
  const { projectId, sourceId } = await params;
  const api = await apiClient(`/projects/${projectId}/sources/jira/${sourceId}`);
  const path = { params: { path: { project_id: projectId } } };
  const sourcePath = { params: { path: { project_id: projectId, source_id: sourceId } } };
  const [project, perms, sources, schemaRes] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
    api.GET("/v1/projects/{project_id}/sources/jira", path),
    api.GET("/v1/projects/{project_id}/sources/jira/{source_id}/schema", sourcePath),
  ]);
  const source = sources.data?.find((s) => s.id === sourceId);
  if (!project.data || !perms.data || !source || !schemaRes.data) notFound();
  const p = project.data;
  const canManage = perms.data.permissions.includes("project.source.manage") && p.status !== "ARCHIVED";
  const schema = schemaRes.data.schema as unknown as Schema;
  const mapping = source.mapping as unknown as Mapping;
  const fieldName = new Map(schema.fields.map((f) => [f.id, f.name]));
  const issues =
    source.status === "ACTIVE"
      ? await api.GET("/v1/projects/{project_id}/sources/jira/{source_id}/issues", sourcePath)
      : null;
  const warnings = source.warnings as { code: string; message: string }[];
  const kept = { issue_types: mapping.overrides?.issue_types ?? {}, statuses: mapping.overrides?.statuses ?? {} };
  const overrideChoice = (name: string) => {
    const o = mapping.overrides?.fields ?? {};
    if (!(name in o)) return "__auto__";
    return o[name] === null ? "__none__" : String(o[name]);
  };
  const fieldOptions = [
    { value: "__auto__", label: "(detect automatically)" },
    { value: "__none__", label: "(do not read)" },
    ...schema.fields
      .filter((f) => f.custom || ["description", "summary"].includes(f.id))
      .map((f) => ({ value: f.id, label: `${f.name} (${f.id})` })),
  ];

  return (
    <main>
      <h1>
        Jira {source.jira_key} · {source.name}
      </h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> /{" "}
        <Link href={`/workspaces/${p.workspace_id}/projects`}>Projects</Link> /{" "}
        <Link href={`/projects/${p.id}`}>{p.key}</Link> / <Link href={`/projects/${p.id}/sources`}>Sources</Link> /{" "}
        {source.jira_key}
      </p>
      <div className="grid">
        <section className="card" aria-labelledby="overview">
          <h2 id="overview">Overview</h2>
          <dl className="facts">
            <dt>Status</dt>
            <dd>
              <span className={`chip ${source.status}`}>{source.status}</span>
              {source.status_reason && <span className="hint"> {source.status_reason}</span>}
            </dd>
            <dt>Site</dt>
            <dd><code>{source.base_url}</code> via {source.connection_name}</dd>
            <dt>Access</dt>
            <dd data-testid="jira-access">
              {(source.capabilities as { write_capable?: boolean }).write_capable ? "read + write" : "read only"}
            </dd>
            <dt>Schema</dt>
            <dd data-testid="schema-version">
              v{source.snapshot_version}, discovered{" "}
              {new Date(schemaRes.data.discovered_at).toLocaleString("en-GB", { timeZone: "UTC" })} UTC
            </dd>
          </dl>
          {warnings.length > 0 && (
            <ul className="issues" data-testid="jira-warnings">
              {warnings.map((w, n) => (
                <li key={n} className="warning">
                  <strong>{w.code}</strong> — {w.message}
                </li>
              ))}
            </ul>
          )}
          {canManage && (
            <ActionForm
              action={rediscoverJira}
              testId="rediscover"
              submitLabel="Rediscover schema"
              hidden={{ project_id: p.id, source_id: source.id }}
              fields={[]}
            />
          )}
        </section>

        <section className="card" aria-labelledby="mapping">
          <h2 id="mapping">Field mapping</h2>
          <table data-testid="field-mapping">
            <thead>
              <tr>
                <th scope="col">INPROOM field</th>
                <th scope="col">Jira field</th>
                <th scope="col">How</th>
              </tr>
            </thead>
            <tbody>
              {CANONICAL_FIELDS.map((c) => {
                const m = mapping.fields[c.name];
                return (
                  <tr key={c.name} data-testid={`mapping-${c.name}`}>
                    <td>{c.label}</td>
                    <td>{m ? `${fieldName.get(m.field_id) ?? m.field_id} (${m.field_id})` : "—"}</td>
                    <td>{m ? m.source : "not mapped"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {canManage && (
            <ActionForm
              action={saveJiraMapping}
              testId="mapping-form"
              submitLabel="Save mapping"
              hidden={{
                project_id: p.id,
                source_id: source.id,
                mapping_version: String(source.mapping_version),
                kept_overrides: JSON.stringify(kept),
              }}
              fields={CANONICAL_FIELDS.map((c) => ({
                name: c.name,
                label: c.label,
                options: fieldOptions,
                defaultValue: overrideChoice(c.name),
              }))}
            />
          )}
        </section>

        <section className="card" aria-labelledby="types">
          <h2 id="types">Issue types and statuses</h2>
          <table data-testid="issue-type-mapping">
            <thead>
              <tr>
                <th scope="col">Jira issue type</th>
                <th scope="col">INPROOM type</th>
              </tr>
            </thead>
            <tbody>
              {schema.issue_types.map((t) => (
                <tr key={t.id}>
                  <td>{t.name}</td>
                  <td><span className="chip">{mapping.issue_types[t.id] ?? t.canonical}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          <table data-testid="status-mapping">
            <thead>
              <tr>
                <th scope="col">Jira status</th>
                <th scope="col">INPROOM status</th>
              </tr>
            </thead>
            <tbody>
              {schema.statuses.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td><span className="chip">{(mapping.statuses[s.id] ?? s.canonical).replace("_", " ")}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          <details>
            <summary>
              Boards ({schema.boards.length}), sprints ({schema.sprints.length}), link types ({schema.link_types.length}),
              custom fields ({schema.fields.filter((f) => f.custom).length})
            </summary>
            <ul>
              {schema.boards.map((b) => (
                <li key={`b${b.id}`}>Board {b.name} ({b.type})</li>
              ))}
              {schema.sprints.map((s) => (
                <li key={`s${s.id}`}>{s.name}</li>
              ))}
              {schema.link_types.map((l) => (
                <li key={`l${l.id}`}>{l.name}: {l.outward} / {l.inward}</li>
              ))}
              {schema.fields.filter((f) => f.custom).map((f) => (
                <li key={`f${f.id}`}>
                  {f.name} <code>{f.id}</code> {f.schema_type && <span className="hint">{f.schema_type}</span>}
                </li>
              ))}
            </ul>
          </details>
        </section>

        <section className="card wide" aria-labelledby="issues">
          <h2 id="issues">Issue preview</h2>
          <p className="hint">Read-only, first page, normalised through the mapping above. Nothing is stored yet.</p>
          {!issues ? (
            <p className="empty">This Jira project is paused, so INPROOM does not read it.</p>
          ) : issues.error ? (
            <p className="alert error">{(issues.error as { detail?: string }).detail}</p>
          ) : (
            <table data-testid="issue-preview">
              <thead>
                <tr>
                  <th scope="col">Key</th>
                  <th scope="col">Type</th>
                  <th scope="col">Status</th>
                  <th scope="col">Parent</th>
                  <th scope="col">Points</th>
                  <th scope="col">Acceptance criteria</th>
                  <th scope="col">Dependencies</th>
                </tr>
              </thead>
              <tbody>
                {(issues.data?.items ?? []).map((i) => (
                  <tr key={i.source_id} data-testid="issue-row" aria-label={i.key}>
                    <td>
                      <strong>{i.key}</strong> {i.summary}
                    </td>
                    <td>{i.type}</td>
                    <td>{i.status.replace("_", " ")} <span className="hint">({i.status_raw})</span></td>
                    <td>{i.parent_key ?? "—"}</td>
                    <td>{i.story_points ?? "—"}</td>
                    <td data-testid="issue-acceptance">{i.acceptance_criteria ?? "—"}</td>
                    <td>
                      {i.links.map((l) => `${l.relation} ${l.target_key}`).join("; ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </main>
  );
}
