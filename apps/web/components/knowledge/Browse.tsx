import Link from "next/link";

import type { components } from "@/lib/api-schema";
import { BROWSE_KINDS, encodeItem, explorerHref, formatTime, HEALTH_CHIP, type ExplorerState } from "@/lib/knowledge";

type SyncSource = components["schemas"]["SourceSyncStatus"];
type Item = components["schemas"]["SourceObject"];
type Snapshot = components["schemas"]["SnapshotInfo"];

/** Left pane (UX-04): sources with freshness, items by kind, saved memory snapshots. */
export function Browse({
  projectId,
  state,
  sources,
  items,
  next,
  snapshots,
}: {
  projectId: string;
  state: ExplorerState;
  sources: SyncSource[];
  items: Item[] | null;
  next: string | null;
  snapshots: Snapshot[];
}) {
  const kind = state.kind ?? "upload";
  return (
    <>
      <section aria-labelledby="browse-sources">
        <h2 id="browse-sources">Sources</h2>
        {sources.length === 0 ? (
          <p className="empty" data-testid="no-sources">
            No sources yet. <Link href={`/projects/${projectId}/sources`}>Connect Jira, Confluence or a repository</Link>,
            or upload documents.
          </p>
        ) : (
          <ul className="plain" data-testid="source-health">
            {sources.map((s) => (
              <li key={s.source_id}>
                <span className="kind">{s.kind}</span> {s.name}{" "}
                <span className={`chip ${HEALTH_CHIP[s.health] ?? "DEGRADED"}`} title={`sync ${s.health}`}>
                  {s.health}
                </span>
                <div className="hint">data as of {formatTime(s.data_as_of)}</div>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="browse-items">
        <h2 id="browse-items">Browse</h2>
        <nav className="tabs" aria-label="Item kinds">
          {BROWSE_KINDS.map((k) => (
            <Link
              key={k.kind}
              href={explorerHref(projectId, state, { kind: k.kind, cursor: undefined })}
              aria-current={k.kind === kind ? "page" : undefined}
            >
              {k.label}
            </Link>
          ))}
        </nav>
        {items === null ? null : items.length === 0 ? (
          <p className="empty" data-testid="no-items">
            Nothing here yet{kind === "upload" ? ": upload documents from the project's sources page" : ""}.
          </p>
        ) : (
          <ul className="plain items" data-testid="item-list">
            {items.map((it) => {
              const ref = encodeItem({ kind: it.source_kind, source_id: it.source_id, external_id: it.external_id });
              const selected = state.item === ref;
              return (
                <li key={it.id}>
                  <Link
                    href={explorerHref(projectId, state, { item: ref, node: undefined, edges: undefined })}
                    aria-current={selected ? "true" : undefined}
                  >
                    {it.title || it.external_id}
                  </Link>
                  {it.current && <span className="hint"> · rev {it.current.seq}</span>}
                </li>
              );
            })}
          </ul>
        )}
        {(state.cursor || next) && (
          <p className="pager">
            {state.cursor && <Link href={explorerHref(projectId, state, { cursor: undefined })}>First page</Link>}
            {state.cursor && next && " · "}
            {next && (
              <Link href={explorerHref(projectId, state, { cursor: next })} data-testid="items-next">
                Next page
              </Link>
            )}
          </p>
        )}
      </section>
      <section aria-labelledby="browse-memory">
        <h2 id="browse-memory">Memory snapshots</h2>
        {snapshots.length === 0 ? (
          <p className="empty">No snapshots yet.</p>
        ) : (
          <ul className="plain" data-testid="snapshot-list">
            {snapshots.map((s) => (
              <li key={s.version}>
                <Link href={`/projects/${projectId}/knowledge/memory/${s.version}`}>Version {s.version}</Link>
                <span className="hint"> · {formatTime(s.created_at)} · {s.reason}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
