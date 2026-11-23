import Link from "next/link";
import { notFound } from "next/navigation";

import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  const api = await apiClient(`/projects/${projectId}`);
  const { data: p, response } = await api.GET("/v1/projects/{project_id}", {
    params: { path: { project_id: projectId } },
  });
  if (!p || response.status === 404 || response.status === 422) notFound();
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
            <dt>Your roles</dt><dd>{p.my_roles.join(", ") || "tenant admin"}</dd>
            <dt>Version</dt><dd>{p.version}</dd>
            <dt>Description</dt><dd>{p.description ?? "—"}</dd>
          </dl>
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
      </div>
    </main>
  );
}
