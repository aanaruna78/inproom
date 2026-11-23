import type { NextRequest } from "next/server";

import { signIn } from "@/auth";
import { safeReturnPath } from "@/lib/return-path";

/** Unauthenticated users land here (see middleware) and are sent straight to Keycloak. */
export async function GET(request: NextRequest) {
  const redirectTo = safeReturnPath(request.nextUrl.searchParams.get("callbackUrl"), request.nextUrl.origin);
  await signIn("keycloak", { redirectTo });
}
