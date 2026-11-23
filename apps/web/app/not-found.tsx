import Link from "next/link";

export default function NotFound() {
  return (
    <main>
      <h1>404 - Not found</h1>
      <p className="crumbs">This page does not exist or you do not have access to it.</p>
      <p>
        <Link href="/workspaces">Back to your workspaces</Link>
      </p>
    </main>
  );
}
