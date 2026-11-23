import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { auth, signOut } from "@/auth";

import "./globals.css";

export const metadata: Metadata = {
  title: "INPROOM",
  description: "Intelligent Process Orchestration & Management",
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const session = await auth();
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <Link href="/" className="brand">
            INPROOM
          </Link>
          {session?.user && <Link href="/workspaces">Workspaces</Link>}
          {session?.user && <Link href="/integrations">Integrations</Link>}
          <span className="spacer" />
          {session?.user ? (
            <>
              <span className="who" data-testid="signed-in-user">
                {session.user.email}
              </span>
              <form
                action={async () => {
                  "use server";
                  await signOut({ redirectTo: "/" });
                }}
              >
                <button type="submit">Sign out</button>
              </form>
            </>
          ) : (
            <Link href="/login">Sign in</Link>
          )}
        </header>
        {children}
      </body>
    </html>
  );
}
