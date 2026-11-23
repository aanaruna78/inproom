import Link from "next/link";
import { notFound } from "next/navigation";

import { createProject } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ProjectsPage({ params }: { params: Promise<{ workspaceId: string }> }) {
  const { workspaceId } = await params;
  const path = `/workspaces/${workspaceId}/projects`;
  const api = await apiClient(path);
  const [list, workspaces] = await Promise.all([
    api.GET("/v1/workspaces/{workspace_id}/projects", {
      params: { path: { workspace_id: workspaceId }, query: { limit: 100 } },
    }),
    api.GET("/v1/workspaces", { params: { query: { limit: 100 } } }),
  ]);
  if (list.response.status === 404 || list.response.status === 422) notFound();
  const workspace = workspaces.data?.items.find((w) => w.id === workspaceId);
  const items = list.data?.items ?? [];
  return (
    <main>
      <h1>{workspace?.name ?? "Workspace"}</h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> / {workspace?.slug} / Projects
      </p>
      <div className="grid">
        <section className="card" aria-labelledby="pj-list">
          <h2 id="pj-list">Projects</h2>
          {items.length === 0 ? (
            <p className="empty">No projects you can see in this workspace yet.</p>
          ) : (
            <table data-testid="project-table">
              <thead>
                <tr><th>Key</th><th>Name</th><th>Mode</th><th>Status</th></tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.id}>
                    <td><Link href={`/projects/${p.id}`}><code>{p.key}</code></Link></td>
                    <td>{p.name}</td>
                    <td>{p.mode}</td>
                    <td><span className={`chip ${p.status}`}>{p.status.replace("_", " ")}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        <section className="card" aria-labelledby="pj-create">
          <h2 id="pj-create">Create project</h2>
          <ActionForm
            testId="create-project"
            action={createProject}
            submitLabel="Create project"
            hidden={{ workspace_id: workspaceId }}
            fields={[
              { name: "key", label: "Key", required: true, pattern: "[A-Z][A-Z0-9]{1,9}",
                hint: "2-10 uppercase letters/digits, e.g. INPROOM." },
              { name: "name", label: "Name", required: true, minLength: 3, maxLength: 120 },
              { name: "description", label: "Description (optional)", maxLength: 2000 },
              { name: "mode", label: "Project type", defaultValue: "NEW", options: [
                { value: "NEW", label: "New project" },
                { value: "MIGRATION", label: "Migrate an existing project" },
              ] },
            ]}
          />
        </section>
      </div>
    </main>
  );
}
