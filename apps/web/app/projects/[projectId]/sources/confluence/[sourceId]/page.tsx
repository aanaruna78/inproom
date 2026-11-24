import Link from "next/link";
import { notFound } from "next/navigation";

import { syncConfluenceSpace } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

const STATUSES = ["CURRENT", "RESTRICTED", "EXCLUDED", "DELETED"] as const;
type Status = (typeof STATUSES)[number];

export default async function ConfluenceSpacePage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string; sourceId: string }>;
  searchParams: Promise<{ status?: string; cursor?: string }>;
}) {
  const { projectId, sourceId } = await params;
  const { status, cursor } = await searchParams;
  const filter = STATUSES.includes(status as Status) ? (status as Status) : undefined;
  const api = await apiClient(`/projects/${projectId}/sources/confluence/${sourceId}`);
  const path = { params: { path: { project_id: projectId } } };
  const [project, perms, sources, catalog] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
    api.GET("/v1/projects/{project_id}/sources/confluence", path),
    api.GET("/v1/projects/{project_id}/sources/confluence/{source_id}/pages", {
      params: { path: { project_id: projectId, source_id: sourceId }, query: { status: filter, cursor } },
    }),
  ]);
  const source = sources.data?.find((s) => s.id === sourceId);
  if (!project.data || !perms.data || !source || !catalog.data) notFound();
  const p = project.data;
  const canManage = perms.data.permissions.includes("project.source.manage") && p.status !== "ARCHIVED";
  const base = `/projects/${p.id}/sources/confluence/${source.id}`;

  return (
    <main>
      <h1>
        Confluence {source.space_key} · {source.name}
      </h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> /{" "}
        <Link href={`/workspaces/${p.workspace_id}/projects`}>Projects</Link> /{" "}
        <Link href={`/projects/${p.id}`}>{p.key}</Link> / <Link href={`/projects/${p.id}/sources`}>Sources</Link> /{" "}
        {source.space_key}
      </p>
      <section className="card wide" aria-labelledby="catalog">
        <h2 id="catalog">Page catalog</h2>
        <p className="hint">
          <span className={`chip ${source.status}`}>{source.status}</span> · {source.page_counts.CURRENT} in scope,{" "}
          {source.page_counts.RESTRICTED} restricted, {source.page_counts.EXCLUDED} excluded,{" "}
          {source.page_counts.DELETED} deleted. Only CURRENT pages are in the sync scope.
        </p>
        {canManage && source.status === "ACTIVE" && (
          <ActionForm
            action={syncConfluenceSpace}
            testId="sync-space"
            submitLabel="Sync now"
            hidden={{ project_id: p.id, source_id: source.id }}
            fields={[]}
          />
        )}
        <nav className="tabs" aria-label="Filter by status" data-testid="status-filter">
          <Link href={base} aria-current={!filter ? "page" : undefined}>All</Link>
          {STATUSES.map((s) => (
            <Link key={s} href={`${base}?status=${s}`} aria-current={filter === s ? "page" : undefined}>
              {s.toLowerCase()} ({source.page_counts[s]})
            </Link>
          ))}
        </nav>
        {catalog.data.items.length === 0 ? (
          <p className="empty">No pages.</p>
        ) : (
          <table data-testid="page-catalog">
            <thead>
              <tr>
                <th scope="col">Page</th>
                <th scope="col">Status</th>
                <th scope="col">Version</th>
                <th scope="col">Labels</th>
              </tr>
            </thead>
            <tbody>
              {catalog.data.items.map((pg) => (
                <tr key={pg.page_id} data-testid="page-row" aria-label={pg.page_id}>
                  <td>
                    <Link href={`${base}/pages/${pg.page_id}`}>{pg.title}</Link>
                    {pg.restricted && <span className="chip warning"> restricted</span>}
                  </td>
                  <td>
                    <span className={`chip ${pg.status}`}>{pg.status}</span>
                    {pg.status_reason && <div className="hint">{pg.status_reason}</div>}
                  </td>
                  <td>v{pg.current_version}</td>
                  <td>{pg.labels.join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {catalog.data.next_cursor && (
          <Link
            data-testid="next-page"
            href={`${base}?${new URLSearchParams({ ...(filter ? { status: filter } : {}), cursor: catalog.data.next_cursor })}`}
          >
            Next page →
          </Link>
        )}
      </section>
    </main>
  );
}
