const DEFAULT_RETURN = "/workspaces";

/** Reduce a requested return URL to a same-origin path; anything else falls back (no open redirect). */
export function safeReturnPath(requested: string | null, origin: string): string {
  if (!requested) return DEFAULT_RETURN;
  try {
    const url = new URL(requested, origin);
    if (url.origin !== origin) return DEFAULT_RETURN;
    const path = `${url.pathname}${url.search}`;
    return path.startsWith("/login") ? DEFAULT_RETURN : path;
  } catch {
    return DEFAULT_RETURN;
  }
}
