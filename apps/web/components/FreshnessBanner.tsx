/** "Data as of …" banner for a source that is not fresh (INPROOM-147 / story INPROOM-26). */
export function FreshnessBanner({
  health,
  dataAsOf,
  reason,
}: {
  health?: string;
  dataAsOf?: string | null;
  reason?: string | null;
}) {
  if (!health || health === "fresh") return null;
  const when = dataAsOf ? new Date(dataAsOf).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC" : "never synced";
  const label: Record<string, string> = {
    stale: "Stale",
    failing: "Sync failing",
    never: "Not synced yet",
    paused: "Paused",
    excluded: "Excluded",
  };
  return (
    <p className="alert warning" role="status" data-testid="freshness-banner">
      <strong>{label[health] ?? health}:</strong> data as of {when}
      {reason ? ` — ${reason}` : ""}. What you see may be out of date.
    </p>
  );
}
