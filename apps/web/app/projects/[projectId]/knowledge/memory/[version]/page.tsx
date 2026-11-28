import Link from "next/link";
import { notFound } from "next/navigation";

import { ProblemAlert } from "@/components/knowledge/ProblemAlert";
import { apiClient, asProblem } from "@/lib/api";
import { formatTime } from "@/lib/knowledge";

export const dynamic = "force-dynamic";

/** A saved memory snapshot (INPROOM-35), as served: lines of items no longer visible are withheld. */
export default async function MemorySnapshot({ params }: { params: Promise<{ projectId: string; version: string }> }) {
  const { projectId, version } = await params;
  const v = Number(version);
  if (!Number.isInteger(v) || v < 1) notFound();
  const api = await apiClient(`/projects/${projectId}/knowledge/memory/${version}`);
  const { data: body, error, response } = await api.GET("/v1/projects/{project_id}/memory/snapshots/{version}", {
    params: { path: { project_id: projectId, version: v } },
  });
  const data = body && typeof body !== "string" ? body : null; // JSON (the default format)
  if (response.status === 404 && (!error || (error as { code?: string }).code !== "MEMORY_SNAPSHOT_NOT_FOUND")) notFound();
  return (
    <main className="explorer">
      <h1>Memory snapshot {version}</h1>
      <p className="crumbs">
        <Link href={`/projects/${projectId}/knowledge`}>Knowledge Explorer</Link> / memory v{version}
      </p>
      {!data ? (
        <ProblemAlert what="Could not open this snapshot" problem={asProblem(error, response.status)} />
      ) : (
        <section className="card" aria-labelledby="snapshot-meta">
          <h2 id="snapshot-meta">
            Version {data.version} · {formatTime(data.created_at)}
          </h2>
          <p className="hint">
            {data.reason} · generator {data.generator} · sha256 <code>{data.sha256.slice(0, 12)}</code>
          </p>
          {data.withheld > 0 && (
            <p className="alert warning" role="status" data-testid="snapshot-withheld">
              {data.withheld} lines are withheld: items they name are no longer visible.
            </p>
          )}
          <pre className="page-text" data-testid="snapshot-markdown">
            {data.markdown}
          </pre>
        </section>
      )}
    </main>
  );
}
