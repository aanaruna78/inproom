import Link from "next/link";
import { notFound } from "next/navigation";

import { addMember } from "@/app/actions";
import { ActionForm } from "@/components/ActionForm";
import { MemberRow } from "@/components/MemberRow";
import { apiClient, PROJECT_ROLES } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function MembersPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  const api = await apiClient(`/projects/${projectId}/members`);
  const path = { params: { path: { project_id: projectId } } };
  const [project, perms, members, me] = await Promise.all([
    api.GET("/v1/projects/{project_id}", path),
    api.GET("/v1/projects/{project_id}/permissions/me", path),
    api.GET("/v1/projects/{project_id}/members", path),
    api.GET("/v1/me"),
  ]);
  if (!project.data || !perms.data || !members.data) notFound();
  const p = project.data;
  // UI convenience only: the API enforces project.member.manage on every change.
  const canManage = perms.data.permissions.includes("project.member.manage");

  return (
    <main>
      <h1>Members of {p.name}</h1>
      <p className="crumbs">
        <Link href="/workspaces">Workspaces</Link> /{" "}
        <Link href={`/workspaces/${p.workspace_id}/projects`}>Projects</Link> /{" "}
        <Link href={`/projects/${p.id}`}>{p.key}</Link> / Members
      </p>
      <div className="grid">
        <section className="card" aria-labelledby="members">
          <h2 id="members">People and roles</h2>
          <table data-testid="member-table">
            <thead>
              <tr>
                <th scope="col">Member</th>
                <th scope="col">Roles</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {members.data.map((m) => (
                <MemberRow
                  key={m.user_id}
                  projectId={p.id}
                  userId={m.user_id}
                  label={m.display_name ?? m.email ?? m.user_id}
                  email={m.email}
                  roles={m.roles}
                  allRoles={PROJECT_ROLES}
                  canManage={canManage}
                  isYou={m.user_id === me.data?.user_id}
                />
              ))}
            </tbody>
          </table>
        </section>
        <section className="card" aria-labelledby="add-member">
          <h2 id="add-member">Add a member</h2>
          {canManage ? (
            <ActionForm
              action={addMember}
              testId="add-member"
              submitLabel="Add member"
              hidden={{ project_id: p.id }}
              fields={[
                {
                  name: "email",
                  label: "Email",
                  required: true,
                  hint: "People appear here after they sign in to INPROOM once.",
                },
                {
                  name: "role",
                  label: "Role",
                  defaultValue: "viewer",
                  options: PROJECT_ROLES.map((r) => ({ value: r, label: r.replaceAll("_", " ") })),
                },
              ]}
            />
          ) : (
            <p className="empty" data-testid="members-read-only">
              Only project admins can add or change members.
            </p>
          )}
        </section>
      </div>
    </main>
  );
}
