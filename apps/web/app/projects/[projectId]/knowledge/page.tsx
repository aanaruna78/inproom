import Link from "next/link";
import { notFound } from "next/navigation";

import { Browse } from "@/components/knowledge/Browse";
import { Inspector, type ProvenanceRecord } from "@/components/knowledge/Inspector";
import { ProblemAlert } from "@/components/knowledge/ProblemAlert";
import { apiClient, asProblem, type Problem } from "@/lib/api";
import { BROWSE_KINDS, decodeItem, explorerHref, type ExplorerState } from "@/lib/knowledge";

export const dynamic = "force-dynamic";

type Selected = { record: ProvenanceRecord; indexedAt: string | null } | { problem: Problem } | null;

/** Knowledge Explorer (UX-04 / INPROOM-36): browse, search and inspect the project's knowledge. Every
 * selection is a URL; the API decides what is visible - the page never shows what it would not serve. */
export default async function KnowledgeExplorer({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<ExplorerState>;
}) {
  const { projectId } = await params;
  const state = await searchParams;
  const kind = BROWSE_KINDS.some((k) => k.kind === state.kind) ? state.kind! : "upload";
  const api = await apiClient(`/projects/${projectId}/knowledge`);
  const path = { params: { path: { project_id: projectId } } };
  const [project, sync, items, snapshots] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/sources/sync-status", path),
    api.GET("/v1/projects/{project_id}/content", {
      params: {
        path: { project_id: projectId },
        query: { kind: kind as "upload", visible: true, limit: 25, cursor: state.cursor },
      },
    }),
    api.GET("/v1/projects/{project_id}/memory/snapshots", {
      params: { path: { project_id: projectId }, query: { limit: 10 } },
    }),
  ]);
  if (!project.data || project.response.status === 404 || project.response.status === 422) notFound();
  const p = project.data;

  const ref = decodeItem(state.item);
  let selected: Selected = null;
  if (ref) selected = await inspect(api, projectId, ref);

  const problems: [string, Problem][] = [];
  if (items.error) problems.push(["Could not list items", asProblem(items.error, items.response.status)]);
  if (sync.error) problems.push(["Could not read source health", asProblem(sync.error, sync.response.status)]);

  return (
    <main className={`explorer ${state.density === "compact" ? "compact" : ""}`}>
      <h1>Knowledge Explorer</h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> / <Link href={`/projects/${p.id}`}>{p.name}</Link> / Knowledge
        <span className="density">
          {" · "}
          <Link href={explorerHref(projectId, state, { density: state.density === "compact" ? undefined : "compact" })}>
            {state.density === "compact" ? "Comfortable density" : "Compact density"}
          </Link>
        </span>
      </p>
      {problems.map(([what, problem]) => (
        <ProblemAlert key={what} what={what} problem={problem} />
      ))}
      <div className="panes">
        <details className="pane browse" open>
          <summary>Browse sources</summary>
          <nav aria-label="Browse knowledge" className="pane-body">
            <Browse
              projectId={projectId}
              state={{ ...state, kind }}
              sources={sync.data?.sources ?? []}
              items={items.data?.items ?? null}
              next={items.data?.next_cursor ?? null}
              snapshots={snapshots.data?.items ?? []}
            />
          </nav>
        </details>
        <section className="pane work" aria-labelledby="work-title" data-testid="work-area">
          <h2 id="work-title">Knowledge</h2>
          <p className="empty">Choose an item in Browse to inspect its source evidence.</p>
        </section>
        <details className="pane inspector" open>
          <summary>Inspector</summary>
          <aside aria-labelledby="inspector-title" className="pane-body" aria-live="polite">
            {selected === null ? (
              <>
                <h2 id="inspector-title">Inspector</h2>
                <p className="empty" data-testid="inspector-empty">
                  Select an item to see its canonical revision, freshness and access scope.
                </p>
              </>
            ) : "problem" in selected ? (
              <>
                <h2 id="inspector-title">Inspector</h2>
                <ProblemAlert what="Could not inspect this item" problem={selected.problem} />
              </>
            ) : (
              <Inspector record={selected.record} indexedAt={selected.indexedAt} />
            )}
          </aside>
        </details>
      </div>
    </main>
  );
}

type Api = Awaited<ReturnType<typeof apiClient>>;

/** Provenance of the item's current revision, and when it was indexed for search (chunks built). */
async function inspect(api: Api, projectId: string, ref: { kind: string; source_id: string; external_id: string }) {
  const item = { project_id: projectId, kind: ref.kind as "upload", source_id: ref.source_id, external_id: ref.external_id };
  const detail = await api.GET("/v1/projects/{project_id}/content/{kind}/{source_id}/{external_id}", {
    params: { path: item },
  });
  if (!detail.data) return { problem: asProblem(detail.error, detail.response.status) };
  const seq = detail.data.current?.seq;
  if (!seq) return { problem: { status: 409, code: "NO_CURRENT_REVISION", title: "No current revision" } as Problem };
  const [resolved, derived] = await Promise.all([
    api.POST("/v1/projects/{project_id}/provenance/resolve", {
      params: { path: { project_id: projectId } },
      body: { citations: [{ kind: ref.kind, source_id: ref.source_id, external_id: ref.external_id, seq }] },
    }),
    api.GET("/v1/projects/{project_id}/content/{kind}/{source_id}/{external_id}/revisions/{seq}/derivatives", {
      params: { path: { ...item, seq } },
    }),
  ]);
  const result = resolved.data?.results[0];
  if (!result?.record) {
    const error = result?.error;
    return {
      problem: error
        ? ({ status: 409, code: error.code, title: "This item cannot be shown", detail: error.detail } as Problem)
        : asProblem(resolved.error, resolved.response.status),
    };
  }
  const chunks = derived.data?.items.find((d) => d.producer === "chunks" && d.status === "READY");
  return { record: result.record as unknown as ProvenanceRecord, indexedAt: chunks?.built_at ?? null };
}
