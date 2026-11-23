import Link from "next/link";

import { auth } from "@/auth";
import { API_URL } from "@/lib/api";

export const dynamic = "force-dynamic";

type Health = { status: string; version: string };

async function fetchHealth(): Promise<{ ok: true; data: Health } | { ok: false; error: string }> {
  try {
    const res = await fetch(`${API_URL}/healthz`, { cache: "no-store", signal: AbortSignal.timeout(2000) });
    if (!res.ok) return { ok: false, error: `API responded ${res.status}` };
    return { ok: true, data: (await res.json()) as Health };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "unreachable" };
  }
}

export default async function Home() {
  const [health, session] = await Promise.all([fetchHealth(), auth()]);
  return (
    <main>
      <h1>INPROOM</h1>
      <p className="crumbs">Intelligent Process Orchestration &amp; Management</p>
      <section className="card" aria-live="polite">
        <strong>API status: </strong>
        {health.ok ? (
          <span style={{ color: "var(--teal)" }}>
            {health.data.status} (v{health.data.version})
          </span>
        ) : (
          <span style={{ color: "var(--red)" }}>unavailable - {health.error}</span>
        )}
      </section>
      <p>
        {session?.user ? (
          <Link href="/workspaces">Go to your workspaces →</Link>
        ) : (
          <Link href="/login">Sign in to manage workspaces and projects →</Link>
        )}
      </p>
    </main>
  );
}
