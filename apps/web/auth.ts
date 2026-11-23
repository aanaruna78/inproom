import NextAuth from "next-auth";
import Keycloak from "next-auth/providers/keycloak";

const issuer = process.env.AUTH_KEYCLOAK_ISSUER ?? "http://localhost:18080/realms/inproom";
const apiUrl = process.env.INPROOM_API_URL ?? "http://localhost:8000";

/** Register the user in the tenant directory (GET /v1/me) so project admins can add them. Best effort. */
async function registerInDirectory(accessToken: string | undefined) {
  if (!accessToken) return;
  try {
    await fetch(`${apiUrl}/v1/me`, { headers: { Authorization: `Bearer ${accessToken}` }, cache: "no-store" });
  } catch {
    // Sign-in must not fail because the API is briefly unavailable; the next sign-in retries.
  }
}

/**
 * OIDC Authorization Code + PKCE against Keycloak (public client `inproom-web`).
 * The access token is kept inside the encrypted session cookie and read only on the server
 * (lib/session.ts); it is deliberately NOT copied into the browser-visible session object.
 */
export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  session: { strategy: "jwt", maxAge: 8 * 60 * 60 },
  pages: { signIn: "/login" },
  providers: [
    Keycloak({
      clientId: process.env.AUTH_KEYCLOAK_ID ?? "inproom-web",
      clientSecret: "",
      issuer,
      client: { token_endpoint_auth_method: "none" },
      checks: ["pkce", "state"],
    }),
  ],
  callbacks: {
    authorized({ auth: session }) {
      return Boolean(session?.user);
    },
    async jwt({ token, account, profile }) {
      if (account) {
        token.accessToken = account.access_token;
        token.expiresAt = account.expires_at;
        token.tenantId = (profile as { tid?: string } | undefined)?.tid;
        await registerInDirectory(account.access_token);
      }
      return token;
    },
    async session({ session, token }) {
      // Expose only non-sensitive identity details to the browser.
      return { ...session, tenantId: token.tenantId as string | undefined };
    },
  },
});
