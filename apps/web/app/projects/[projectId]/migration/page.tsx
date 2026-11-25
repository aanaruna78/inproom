import Link from "next/link";
import { notFound } from "next/navigation";

import { confirmMigrationBaseline, saveMigrationPlan, startMigrationScan } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { apiClient } from "@/lib/api";

export const dynamic = "force-dynamic";

type Example = { key: string; id: string; parent_key?: string; type?: string; targets?: string[]; original_key?: string };
const FLAG_HELP: Record<string, string> = {
  orphan: "Parent not in the inventory (deleted, moved, or not visible to this account). Not re-parented.",
  unsupported_type: "Issue type outside epic / story / task / sub-task / bug. Kept as-is, typed 'other'.",
  unresolved_link: "Links to issues outside this Jira project or not visible. Recorded, not followed.",
  key_changed: "The key changed since first seen (moved or re-keyed). The original key is kept.",
  duplicate_key: "Two different issues carry the same key in the inventory.",
};
const READINESS: Record<string, string> = {
  NO_BASELINE: "no baseline confirmed yet",
  SCAN_REQUIRED: "no scan yet",
  SCAN_FAILED: "the last scan failed",
  SCAN_IN_PROGRESS: "a scan is running",
  SCAN_STALE: "the last scan is older than 24 hours",
  SOURCE_PAUSED: "the Jira source is paused",
  PERMISSION_LOST: "the account lost access",
};

export default async function MigrationPage({
  params,
  searchParams,
}: {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<{ parent?: string; flag?: string }>;
}) {
  const { projectId } = await params;
  const { parent, flag } = await searchParams;
  const api = await apiClient(`/projects/${projectId}/migration`);
  const path = { params: { path: { project_id: projectId } } };
  const [project, perms, migration, jira] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
    api.GET("/v1/projects/{project_id}/migration", path),
    api.GET("/v1/projects/{project_id}/sources/jira", path),
  ]);
  if (!project.data || !perms.data || !migration.data) notFound();
  const p = project.data;
  const m = migration.data;
  const canManage = perms.data.permissions.includes("project.source.manage") && p.status !== "ARCHIVED";
  const run = m.latest_run;
  const baselined = m.plan?.status === "BASELINED";
  const scanned = run?.status === "SUCCEEDED";
  const counts = (run?.counts ?? {}) as { issues?: number; by_type?: Record<string, number>; by_status?: Record<string, number>; flags?: Record<string, number>; removed?: number };
  const diagnostics = (run?.diagnostics ?? {}) as Record<string, Example[]>;
  const [tree, flagged, drift] = await Promise.all([
    scanned || baselined
      ? api.GET("/v1/projects/{project_id}/migration/issues", {
          params: { path: { project_id: projectId }, query: { parent: parent ?? "root", limit: 100 } },
        })
      : Promise.resolve(null),
    flag
      ? api.GET("/v1/projects/{project_id}/migration/issues", {
          params: { path: { project_id: projectId }, query: { flag: flag as "orphan", limit: 100 } },
        })
      : Promise.resolve(null),
    baselined ? api.GET("/v1/projects/{project_id}/migration/drift", path) : Promise.resolve(null),
  ]);
  const base = `/projects/${p.id}/migration`;
  const readiness = m.write_readiness as { ready?: boolean; reasons?: string[] };

  if (m.project_mode !== "MIGRATION") {
    return (
      <main>
        <h1>Migration</h1>
        <p className="empty">This is a NEW project. Migration onboarding applies to projects created in MIGRATION mode.</p>
      </main>
    );
  }

  return (
    <main>
      <h1>
        Migrate {p.name} <span className={`chip ${p.status}`}>{p.status}</span>
      </h1>
      <p className="crumbs">
        <Link href={`/projects/${p.id}`}>{p.key}</Link> / Migration
      </p>
      <p className={`alert ${readiness.ready ? "success" : "warning"}`} data-testid="write-readiness">
        {readiness.ready
          ? "Baseline confirmed and current: future automated changes (E07) may build on it."
          : `Automated Jira changes are blocked: ${(readiness.reasons ?? []).map((r) => READINESS[r] ?? r).join(", ")}.`}
      </p>

      <div className="grid">
        <section className="card" aria-labelledby="step1">
          <h2 id="step1">1. Source of truth</h2>
          <p className="hint">
            Jira stays authoritative: INPROOM inventories the existing Epics, Stories and Sub-tasks with their original
            keys, statuses and authors. Nothing is re-created or re-numbered.
          </p>
          {m.plan && (
            <p data-testid="plan-summary">
              <strong>{m.plan.jira_key}</strong> · {m.plan.history_since ? `issues updated since ${m.plan.history_since}` : "full history"} ·{" "}
              <span className="chip">{m.plan.status}</span>
            </p>
          )}
          {canManage && !baselined && ((jira.data ?? []).length === 0 ? (
            <p className="empty">
              Select the existing Jira project on <Link href={`/projects/${p.id}/sources`}>Sources</Link> first.
            </p>
          ) : (
            <ActionForm
              action={saveMigrationPlan}
              testId="migration-plan"
              submitLabel={m.plan ? "Update plan" : "Save plan"}
              hidden={{ project_id: p.id, version: m.plan ? String(m.plan.version) : "" }}
              fields={[
                {
                  name: "jira_source_id",
                  label: "Existing Jira project",
                  options: (jira.data ?? []).map((j) => ({ value: j.id, label: `${j.jira_key} · ${j.name}` })),
                  defaultValue: m.plan?.jira_source_id,
                },
                {
                  name: "history_since",
                  label: "Only issues updated since (optional, YYYY-MM-DD)",
                  defaultValue: m.plan?.history_since ?? "",
                  placeholder: "empty = full history",
                },
              ]}
            />
          ))}
        </section>

        <section className="card" aria-labelledby="step2">
          <h2 id="step2">2. Scan</h2>
          {!run ? (
            <p className="empty">No scan yet.</p>
          ) : (
            <dl className="facts" data-testid="scan-status">
              <dt>Status</dt><dd><span className={`chip ${run.status === "SUCCEEDED" ? "ACTIVE" : run.status === "FAILED" ? "INVALID" : "DEGRADED"}`}>{run.status}</span></dd>
              <dt>Progress</dt><dd>{run.pages_done} page(s), {run.issues_seen} issue(s) read</dd>
              <dt>As</dt><dd>{String((run.scanned_as as { account?: string }).account ?? "—")}</dd>
              {run.error && <><dt>Last error</dt><dd className="warning">{String((run.error as { message?: string }).message)}</dd></>}
            </dl>
          )}
          {canManage && m.plan && (
            <ActionForm
              action={startMigrationScan}
              testId="migration-scan"
              submitLabel={baselined ? "Re-sync" : run ? "Scan again" : "Scan"}
              hidden={{ project_id: p.id }}
              fields={[]}
            />
          )}
        </section>

        {(scanned || baselined) && (
          <section className="card wide" aria-labelledby="step3">
            <h2 id="step3">3. Preview</h2>
            <p data-testid="inventory-counts">
              <strong>{counts.issues ?? 0}</strong> issues ·{" "}
              {Object.entries(counts.by_type ?? {}).map(([t, n]) => `${n} ${t}`).join(", ") || "none"}
              {counts.removed ? ` · ${counts.removed} removed since first seen` : ""}
            </p>
            <h3>Hierarchy {parent && <Link href={base}>(back to top level)</Link>}</h3>
            <table data-testid="inventory-tree">
              <thead>
                <tr><th scope="col">Key</th><th scope="col">Type</th><th scope="col">Status</th><th scope="col">Summary</th><th scope="col">Children</th></tr>
              </thead>
              <tbody>
                {(tree?.data?.items ?? []).map((i) => (
                  <tr key={i.jira_issue_id} data-testid="inventory-row" aria-label={i.key}>
                    <td>
                      <strong>{i.key}</strong>
                      {i.original_key !== i.key && <div className="hint">was {i.original_key}</div>}
                    </td>
                    <td>{i.type}{i.type_raw.toLowerCase().replace("-", "") !== i.type && <span className="hint"> ({i.type_raw})</span>}</td>
                    <td>{i.status_raw}</td>
                    <td>{i.summary}{i.flags.length > 0 && <div className="hint warning">{i.flags.join(", ")}</div>}</td>
                    <td>{i.children > 0 ? <Link href={`${base}?parent=${i.jira_issue_id}`}>{i.children} →</Link> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <h3>Diagnostics (reported, never invented)</h3>
            {Object.keys(counts.flags ?? {}).length === 0 ? (
              <p className="hint">Nothing unresolved.</p>
            ) : (
              <ul className="issues" data-testid="diagnostics">
                {Object.entries(counts.flags ?? {}).map(([name, n]) => (
                  <li key={name} className="warning">
                    <Link href={`${base}?flag=${name}`}><strong>{name}</strong> ({n})</Link> — {FLAG_HELP[name] ?? ""}
                    <div className="hint">
                      {(diagnostics[name] ?? []).slice(0, 5).map((x) => x.key + (x.parent_key ? ` → ${x.parent_key}` : "") + (x.targets ? ` → ${x.targets.join(", ")}` : "") + (x.type ? ` (${x.type})` : "")).join(" · ")}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {flagged?.data && (
              <ul data-testid="flagged">
                {flagged.data.items.map((i) => <li key={i.jira_issue_id}>{i.key} — {i.summary}</li>)}
              </ul>
            )}
          </section>
        )}

        <section className="card" aria-labelledby="step4">
          <h2 id="step4">4. Baseline</h2>
          {baselined ? (
            <dl className="facts" data-testid="baseline">
              {(() => {
                const b = m.plan?.baseline as { counts?: { issues?: number }; confirmed_by?: string; confirmed_at?: string; warnings?: string[] };
                return (
                  <>
                    <dt>Issues</dt><dd>{b.counts?.issues ?? 0}</dd>
                    <dt>Confirmed</dt><dd>{b.confirmed_by} · {b.confirmed_at ? new Date(b.confirmed_at).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : ""}</dd>
                    {b.warnings && b.warnings.length > 0 && <><dt>Warnings</dt><dd>{b.warnings.join(", ")}</dd></>}
                  </>
                );
              })()}
            </dl>
          ) : scanned && canManage ? (
            <>
              <p className="hint">
                Confirm once the preview matches your Jira project. This records the baseline and activates the project; the
                plan cannot change afterwards. Re-syncs then report drift against it.
              </p>
              <ActionForm
                action={confirmMigrationBaseline}
                testId="migration-confirm"
                submitLabel="Confirm baseline"
                hidden={{ project_id: p.id, version: String(m.plan?.version ?? "") }}
                fields={[]}
              />
            </>
          ) : (
            <p className="empty">Scan and review the preview first.</p>
          )}
          {drift?.data && (
            <div data-testid="drift">
              <h3>Changes since the baseline</h3>
              <p>
                {drift.data.counts.added} added · {drift.data.counts.changed} changed · {drift.data.counts.removed} removed
              </p>
              <ul>
                {drift.data.changed.slice(0, 10).map((c) => (
                  <li key={String(c.id)}>
                    {String(c.key)}: {Object.keys(c.changes as object).join(", ")}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
