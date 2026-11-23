"use client";

import { useActionState, useEffect, useRef, useState } from "react";

import { removeMember, setMemberRoles } from "@/app/actions";
import { initialFormState } from "@/lib/form-state";

type Props = {
  projectId: string;
  userId: string;
  label: string;
  email: string | null;
  roles: string[];
  allRoles: readonly string[];
  canManage: boolean;
  isYou: boolean;
};

/** One member: roles as a checkbox set (when the viewer may manage members) plus a two-step remove. */
export function MemberRow({ projectId, userId, label, email, roles, allRoles, canManage, isYou }: Props) {
  const [saveState, saveAction, saving] = useActionState(setMemberRoles, initialFormState);
  const [removeState, removeAction, removing] = useActionState(removeMember, initialFormState);
  const [confirming, setConfirming] = useState(false);
  const alertRef = useRef<HTMLDivElement>(null);
  const state = removeState.status !== "idle" ? removeState : saveState;
  const testId = "member";

  useEffect(() => {
    if (state.status === "error") alertRef.current?.focus();
    if (removeState.status !== "idle") setConfirming(false);
  }, [state, removeState]);

  return (
    <tr data-testid={testId} data-user-id={userId}>
      <td>
        <strong>{label}</strong>
        {isYou && <span className="hint"> (you)</span>}
        <div className="hint">{email ?? userId}</div>
      </td>
      <td>
        {canManage ? (
          <form action={saveAction} className="roles" aria-label={`Roles for ${label}`}>
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="user_id" value={userId} />
            <fieldset>
              <legend className="sr-only">Roles for {label}</legend>
              {allRoles.map((role) => (
                <label key={role} className="check">
                  <input type="checkbox" name="roles" value={role} defaultChecked={roles.includes(role)} />
                  {role.replaceAll("_", " ")}
                </label>
              ))}
            </fieldset>
            <button type="submit" disabled={saving}>
              {saving ? "Saving…" : "Save roles"}
            </button>
          </form>
        ) : (
          <span data-testid={`${testId}-roles`}>{roles.join(", ")}</span>
        )}
        {state.status !== "idle" && (
          <div
            ref={alertRef}
            tabIndex={-1}
            role={state.status === "error" ? "alert" : "status"}
            className={`alert ${state.status}`}
            data-testid={`${testId}-${state.status}`}
          >
            {state.message}
          </div>
        )}
      </td>
      <td>
        {canManage &&
          (confirming ? (
            <form action={removeAction} className="inline">
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="user_id" value={userId} />
              <button type="submit" className="danger" disabled={removing}>
                {removing ? "Removing…" : `Confirm remove`}
              </button>
              <button type="button" onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </form>
          ) : (
            <button type="button" onClick={() => setConfirming(true)}>
              Remove
            </button>
          ))}
      </td>
    </tr>
  );
}
