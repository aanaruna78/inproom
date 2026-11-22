export const dynamic = "force-dynamic";

type Health = { status: string; version: string };

async function fetchHealth(): Promise<{ ok: true; data: Health } | { ok: false; error: string }> {
  const base = process.env.INPROOM_API_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${base}/healthz`, { cache: "no-store", signal: AbortSignal.timeout(2000) });
    if (!res.ok) return { ok: false, error: `API responded ${res.status}` };
    return { ok: true, data: (await res.json()) as Health };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "unreachable" };
  }
}

export default async function Home() {
  const health = await fetchHealth();
  return (
    <main style={{ maxWidth: 720, margin: "64px auto", padding: "0 16px" }}>
      <h1 style={{ color: "#142940", marginBottom: 4 }}>INPROOM</h1>
      <p style={{ color: "#66758B", marginTop: 0 }}>Intelligent Process Orchestration &amp; Management</p>
      <section
        aria-live="polite"
        style={{ background: "#FFFFFF", border: "1px solid #E4EAF2", borderRadius: 12, padding: 16 }}
      >
        <strong>API status: </strong>
        {health.ok ? (
          <span style={{ color: "#0D9488" }}>
            {health.data.status} (v{health.data.version})
          </span>
        ) : (
          <span style={{ color: "#C64747" }}>unavailable - {health.error}</span>
        )}
      </section>
    </main>
  );
}
