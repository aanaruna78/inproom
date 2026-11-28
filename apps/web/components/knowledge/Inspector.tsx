import type { ReactNode } from "react";

import { formatTime, HEALTH_CHIP } from "@/lib/knowledge";

/** The provenance record of a cited revision (POST /provenance/resolve; INPROOM-182..184). */
export type ProvenanceRecord = {
  source: { kind: string; name: string | null; item_id: string; title: string; url: string | null };
  revision: {
    seq: number;
    provider_version: string | null;
    content_sha256: string;
    retrieved_at: string | null;
    verified_ref: string | null;
    content_stored: boolean;
  };
  modified: { at: string | null; by: string | null };
  trust: { tier: string; classification: string };
  freshness: {
    status: string;
    reasons: { code: string; detail: string }[];
    current_seq: number | null;
    source_sync?: { health: string | null; data_as_of: string | null };
  };
  completeness: { score: number; missing: { field: string; reason: string }[] };
};

/** Right pane (UX-04): source evidence for the selected item. */
export function Inspector({
  record,
  indexedAt,
  children,
}: {
  record: ProvenanceRecord;
  indexedAt: string | null;
  children?: ReactNode;
}) {
  const { source, revision, freshness, trust, modified } = record;
  const sync = freshness.source_sync;
  return (
    <div data-testid="inspector">
      <h2 id="inspector-title">{source.title || source.item_id}</h2>
      <p>
        <span className={`chip ${HEALTH_CHIP[freshness.status] ?? "DEGRADED"}`} data-testid="item-freshness">
          {freshness.status}
        </span>{" "}
        <span className="hint">
          {source.kind}
          {source.name ? ` · ${source.name}` : ""}
        </span>
      </p>
      {freshness.reasons.length > 0 && (
        <ul className="issues" data-testid="freshness-reasons">
          {freshness.reasons.map((r) => (
            <li key={r.code} className="warning">
              <code>{r.code}</code> {r.detail}
            </li>
          ))}
        </ul>
      )}
      <h3>Canonical source revision</h3>
      <dl className="facts" data-testid="revision-facts">
        <dt>Revision</dt>
        <dd>
          {revision.seq}
          {freshness.current_seq && freshness.current_seq !== revision.seq ? ` (current: ${freshness.current_seq})` : ""}
        </dd>
        <dt>Provider version</dt>
        <dd>{revision.provider_version ?? "—"}</dd>
        {revision.verified_ref && (
          <>
            <dt>Commit</dt>
            <dd>
              <code>{revision.verified_ref.slice(0, 12)}</code>
            </dd>
          </>
        )}
        <dt>Content hash</dt>
        <dd>
          <code title={revision.content_sha256}>{revision.content_sha256.slice(0, 12)}</code>
        </dd>
        <dt>Last retrieved</dt>
        <dd data-testid="retrieved-at">{formatTime(revision.retrieved_at)}</dd>
        <dt>Last indexed</dt>
        <dd data-testid="indexed-at">{indexedAt ? formatTime(indexedAt) : "not indexed for search yet"}</dd>
        <dt>Changed</dt>
        <dd>
          {formatTime(modified.at)}
          {modified.by ? ` by ${modified.by}` : ""}
        </dd>
      </dl>
      <h3>Access scope</h3>
      <dl className="facts" data-testid="access-scope">
        <dt>Trust</dt>
        <dd>{trust.tier}</dd>
        <dt>Classification</dt>
        <dd>{trust.classification}</dd>
        <dt>Source sync</dt>
        <dd>
          {sync?.health ?? "n/a"}
          {sync?.data_as_of ? ` · data as of ${formatTime(sync.data_as_of)}` : ""}
        </dd>
      </dl>
      {children}
      <p>
        {source.url ? (
          <a href={source.url} target="_blank" rel="noreferrer noopener" data-testid="open-original">
            Open original source<span className="sr-only"> (opens in a new tab)</span>
          </a>
        ) : (
          <span className="hint" data-testid="no-original">
            No provider location (for example an uploaded document).
          </span>
        )}
      </p>
    </div>
  );
}
