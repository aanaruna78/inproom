import "server-only";

import { getToken } from "next-auth/jwt";
import { headers } from "next/headers";
import { redirect } from "next/navigation";

/** Read the API access token from the encrypted session cookie (server only). Expired => sign in again. */
export async function requireAccessToken(returnTo: string): Promise<string> {
  const token = await getToken({
    req: { headers: await headers() },
    secret: process.env.AUTH_SECRET,
    secureCookie: process.env.NODE_ENV === "production" && process.env.AUTH_URL?.startsWith("https://"),
  });
  const accessToken = token?.accessToken as string | undefined;
  const expiresAt = token?.expiresAt as number | undefined;
  if (!accessToken || (expiresAt && expiresAt * 1000 < Date.now() + 10_000)) {
    redirect(`/login?callbackUrl=${encodeURIComponent(returnTo)}`);
  }
  return accessToken;
}
