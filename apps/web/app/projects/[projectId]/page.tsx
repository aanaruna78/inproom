import Link from "next/link";
import { notFound } from "next/navigation";

import { archiveProject, updateProject } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  const api = await apiClient(`/projects/${projectId}`);
  const path = { params: { path: { project_id: projectId } } };
  const [{ data: p, response }, { data: perms }] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
  ]);
  if (!p || !perms || response.status === 404 || response.status === 422) notFound();
  // Controls are shown only when the API says they are allowed; the API still enforces every action.
  const can = (permission: string) => perms.permissions.includes(permission);
  const archived = p.status === "ARCHIVED";

  return (
    <main>
      <h1>
        {p.name} <span className={`chip ${p.status}`}>{p.status.replace("_", " ")}</span>
      </h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> /{" "}
        <Link href={`/workspaces/${p.workspace_id}/projects`}>Projects</Link> / {p.key}
      </p>
      <div className="grid">
        <section className="card" aria-labelledby="facts">
          <h2 id="facts">Project</h2>
          <dl className="facts" data-testid="project-facts">
            <dt>Key</dt><dd><code>{p.key}</code></dd>
            <dt>Type</dt><dd>{p.mode === "MIGRATION" ? "Migration of an existing project" : "New project"}</dd>
            <dt>Status</dt><dd>{p.status}</dd>
            <dt>Owner</dt><dd>{p.owner_id}</dd>
            <dt>Your roles</dt>
            <dd data-testid="my-roles">
              {perms.roles.join(", ") || "—"}
              {perms.tenant_roles.length > 0 && <span className="hint"> (tenant: {perms.tenant_roles.join(", ")})</span>}
            </dd>
            <dt>Version</dt><dd>{p.version}</dd>
            <dt>Description</dt><dd>{p.description ?? "—"}</dd>
          </dl>
          <p>
            <Link href={`/projects/${p.id}/members`} data-testid="members-link">
              {can("project.member.manage") ? "Manage members" : "View members"}
            </Link>
          </p>
        </section>
        <section className="card" aria-labelledby="settings">
          <h2 id="settings">Settings v{p.settings_version}</h2>
          <dl className="facts">
            {Object.entries(p.settings).map(([k, v]) => (
              <div key={k} style={{ display: "contents" }}>
                <dt>{k.replaceAll("_", " ")}</dt>
                <dd><code>{String(v)}</code></dd>
              </div>
            ))}
          </dl>
        </section>
        {can("project.update") && !archived && (
          <section className="card" aria-labelledby="edit">
            <h2 id="edit">Edit project</h2>
            <ActionForm
              action={updateProject}
              testId="edit-project"
              submitLabel="Save changes"
              hidden={{ project_id: p.id, version: String(p.version) }}
              fields={[
                { name: "name", label: "Name", required: true, minLength: 3, maxLength: 120, defaultValue: p.name },
                { name: "description", label: "Description", maxLength: 2000, defaultValue: p.description ?? "" },
              ]}
            />
          </section>
        )}
        {can("project.archive") && !archived && (
          <section className="card" aria-labelledby="archive">
            <h2 id="archive">Archive</h2>
            <p className="hint">Archived projects are read-only and stop counting toward the workspace limit.</p>
            <ActionForm
              action={archiveProject}
              testId="archive-project"
              submitLabel="Archive project"
              hidden={{ project_id: p.id }}
              fields={[]}
            />
          </section>
        )}
        {!can("project.update") && (
          <p className="hint" data-testid="read-only-note">
            You have read-only access to this project.
          </p>
        )}
      </div>
    </main>
  );
}
