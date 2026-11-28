import Link from "next/link";
import { notFound } from "next/navigation";

import { changeLifecycle, updateProject } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

// Mirrors the API's transition table (INPROOM-103) to decide which buttons to show; the API decides.
const LIFECYCLE: Record<string, ("activate" | "archive" | "restore")[]> = {
  PENDING_SOURCES: ["activate", "archive"],
  ACTIVE: ["archive"],
  ARCHIVED: ["restore"],
};

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
            <Link href={`/projects/${p.id}/sources`} data-testid="sources-link">
              Sources
            </Link>
            {" · "}
            <Link href={`/projects/${p.id}/knowledge`} data-testid="knowledge-link">
              Knowledge
            </Link>
            {" · "}
            <Link href={`/projects/${p.id}/members`} data-testid="members-link">
              {can("project.member.manage") ? "Manage members" : "View members"}
            </Link>
            {p.mode === "MIGRATION" && (
              <>
                {" · "}
                <Link href={`/projects/${p.id}/migration`} data-testid="migration-link">
                  Migration{p.status === "PENDING_SOURCES" ? " (set up the baseline)" : ""}
                </Link>
              </>
            )}
          </p>
        </section>
        <section className="card" aria-labelledby="settings">
          <h2 id="settings">Settings v{p.settings_version}</h2>
          <dl className="facts" data-testid="settings-summary">
            <dt>Process</dt><dd><code>{p.settings.process_package}</code></dd>
            <dt>Write mode</dt><dd><code>{p.settings.write_mode}</code></dd>
            <dt>Approvals</dt><dd>{p.settings.approvals?.required_scopes?.join(", ") || "none"}</dd>
            <dt>Production</dt><dd>{p.settings.production?.actions_enabled ? "on" : "off"}</dd>
          </dl>
          <p>
            <Link href={`/projects/${p.id}/settings`} data-testid="settings-link">
              {can("project.settings.update") && !archived ? "Edit settings and history" : "View settings and history"}
            </Link>
          </p>
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
        {can("project.archive") && LIFECYCLE[p.status]?.length > 0 && (
          <section className="card" aria-labelledby="lifecycle">
            <h2 id="lifecycle">Lifecycle</h2>
            <p className="hint">
              Archived projects are read-only and don&apos;t count toward the workspace limit. Restoring makes the
              project editable again.
            </p>
            {LIFECYCLE[p.status].map((action) => (
              <ActionForm
                key={action}
                action={changeLifecycle}
                testId={`lifecycle-${action}`}
                submitLabel={`${action[0].toUpperCase()}${action.slice(1)} project`}
                hidden={{ project_id: p.id, action }}
                fields={[{ name: "reason", label: "Reason (optional)", maxLength: 500 }]}
              />
            ))}
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
