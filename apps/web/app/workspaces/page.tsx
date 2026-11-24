import Link from "next/link";

import { createWorkspace } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function WorkspacesPage({ searchParams }: { searchParams: Promise<{ cursor?: string }> }) {
  const { cursor } = await searchParams;
  const api = await apiClient("/workspaces");
  const { data, error } = await api.GET("/v1/workspaces", { params: { query: { limit: 100, cursor: cursor || undefined } } });
  const items = data?.items ?? [];
  return (
    <main>
      <h1>Workspaces</h1>
      <p className="crumbs">Workspaces group the projects of your organisation.</p>
      <div className="grid">
        <section className="card" aria-labelledby="ws-list">
          <h2 id="ws-list">Your workspaces</h2>
          {error ? (
            <div role="alert" className="alert error">Workspaces could not be loaded.</div>
          ) : items.length === 0 ? (
            <p className="empty">No workspaces yet - create the first one.</p>
          ) : (
            <table data-testid="workspace-table">
              <thead>
                <tr><th>Name</th><th>Slug</th><th>Created</th></tr>
              </thead>
              <tbody>
                {items.map((w) => (
                  <tr key={w.id}>
                    <td><Link href={`/workspaces/${w.id}/projects`}>{w.name}</Link></td>
                    <td><code>{w.slug}</code></td>
                    <td>{new Date(w.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {(cursor || data?.next_cursor) && (
            <nav className="tabs" aria-label="Workspace pages">
              {cursor && <Link href="/workspaces">« First page</Link>}
              {data?.next_cursor && (
                <Link data-testid="workspaces-next" href={`/workspaces?cursor=${encodeURIComponent(data.next_cursor)}`}>
                  Next page »
                </Link>
              )}
            </nav>
          )}
        </section>
        <section className="card" aria-labelledby="ws-create">
          <h2 id="ws-create">Create workspace</h2>
          <ActionForm
            testId="create-workspace"
            action={createWorkspace}
            submitLabel="Create workspace"
            fields={[
              { name: "name", label: "Name", required: true, maxLength: 120 },
              { name: "slug", label: "Slug", required: true, pattern: "[a-z][a-z0-9-]{1,39}",
                hint: "Lowercase letters, digits and dashes; starts with a letter." },
            ]}
          />
        </section>
      </div>
    </main>
  );
}
