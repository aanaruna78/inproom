import Link from "next/link";
import { notFound } from "next/navigation";

import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

type Acl = { users: string[]; groups: string[] };
type Restrictions = { own?: Acl; inherited?: (Acl & { page_id: string; title: string })[]; restricted?: boolean };
type Attachment = { id: string; title: string; media_type: string | null; size: number | null; version: number | null };

export default async function ConfluencePageView({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string; sourceId: string; pageId: string }>;
  searchParams: Promise<{ version?: string }>;
}) {
  const { projectId, sourceId, pageId } = await params;
  const { version } = await searchParams;
  const api = await apiClient(`/projects/${projectId}/sources/confluence/${sourceId}/pages/${pageId}`);
  const pagePath = { params: { path: { project_id: projectId, source_id: sourceId, page_id: pageId } } };
  const [project, detail] = await Promise.all([
    api.GET("/v1/projects/{project_id}", { params: { path: { project_id: projectId } } }),
    api.GET("/v1/projects/{project_id}/sources/confluence/{source_id}/pages/{page_id}", pagePath),
  ]);
  if (!project.data || !detail.data) notFound();
  const p = project.data;
  const page = detail.data;
  const wanted = version && /^\d+$/.test(version) ? Number(version) : undefined;
  const content =
    page.status === "CURRENT"
      ? await api.GET("/v1/projects/{project_id}/sources/confluence/{source_id}/pages/{page_id}/content", {
          params: { ...pagePath.params, query: { version: wanted } },
        })
      : null;
  const acl = page.restrictions as Restrictions;
  const attachments = page.attachments as Attachment[];
  const base = `/projects/${p.id}/sources/confluence/${sourceId}`;

  return (
    <main>
      <h1>{page.title}</h1>
      <p className="crumbs">
        <Link href={`/projects/${p.id}`}>{p.key}</Link> / <Link href={`/projects/${p.id}/sources`}>Sources</Link> /{" "}
        <Link href={base}>Catalog</Link> / {page.title}
      </p>
      <div className="grid">
        <section className="card" aria-labelledby="content">
          <h2 id="content">Content</h2>
          {!content ? (
            <p className="empty" data-testid="page-not-readable">
              Not read: this page is {page.status.toLowerCase()} ({page.status_reason}).
            </p>
          ) : content.error ? (
            <p className="alert error">{(content.error as { detail?: string }).detail}</p>
          ) : (
            <>
              <p className="hint" data-testid="content-version">
                Version {content.data?.version} of {content.data?.current_version} · read live from Confluence, not stored
              </p>
              <pre className="page-text" data-testid="page-content">{content.data?.text}</pre>
            </>
          )}
        </section>
        <section className="card" aria-labelledby="meta">
          <h2 id="meta">Details</h2>
          <dl className="facts">
            <dt>Status</dt>
            <dd><span className={`chip ${page.status}`}>{page.status}</span></dd>
            <dt>Labels</dt>
            <dd>{page.labels.join(", ") || "—"}</dd>
            <dt>Source</dt>
            <dd>{page.web_url ? <a href={page.web_url}>Open in Confluence</a> : "—"}</dd>
          </dl>
          <h3>Versions seen</h3>
          <ul data-testid="page-versions">
            {page.versions.map((v) => (
              <li key={v.version}>
                <Link href={`${base}/pages/${page.page_id}?version=${v.version}`}>v{v.version}</Link> by {v.author ?? "?"}
                {v.message && <> — {v.message}</>}
              </li>
            ))}
          </ul>
          <h3>Read restrictions</h3>
          <div data-testid="page-acl">
            {!acl.restricted ? (
              <p>None: readable by everyone who can read the space.</p>
            ) : (
              <ul>
                {(acl.own?.users.length || acl.own?.groups.length) && (
                  <li>
                    This page: {[...(acl.own?.groups ?? []).map((g) => `group ${g}`), ...(acl.own?.users ?? [])].join(", ")}
                  </li>
                )}
                {(acl.inherited ?? []).map((i) => (
                  <li key={i.page_id}>
                    Inherited from {i.title}: {[...i.groups.map((g) => `group ${g}`), ...i.users].join(", ")}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <h3>Attachments</h3>
          <ul data-testid="page-attachments">
            {attachments.length === 0 && <li>None</li>}
            {attachments.map((a) => (
              <li key={a.id}>
                {a.title} <span className="hint">{a.media_type ?? "?"}, {a.size ?? "?"} bytes</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </main>
  );
}
