export { auth as middleware } from "@/auth";

// Pages that require a signed-in user. Unauthenticated requests are redirected to /login -> Keycloak.
export const config = {
  matcher: ["/workspaces/:path*", "/projects/:path*"],
};
