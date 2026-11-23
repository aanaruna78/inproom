"use client";

import { useActionState, useEffect, useRef } from "react";

import { saveSettings } from "@/app/actions";
import { initialFormState } from "@/lib/form-state";

type Settings = {
  process_package: string;
  write_mode: string;
  model_policy: string;
  approvals: { required_scopes: string[]; min_approvers: number; expiry_hours: number };
  sources: { allowed_kinds: string[]; max_classification: string; approve_new_sources: boolean };
  production: { actions_enabled: boolean; environments: string[] };
};

type Props = {
  projectId: string;
  version: number;
  settings: Settings;
  approvalScopes: readonly string[];
  sourceKinds: readonly string[];
  /** Only a tenant admin may turn production actions on or change them while on (the API enforces this). */
  canManageProduction: boolean;
};

const label = (v: string) => v.replaceAll("_", " ");

/** Settings editor. Every value is validated again by the API; errors come back inline with their field. */
export function SettingsForm({ projectId, version, settings, approvalScopes, sourceKinds, canManageProduction }: Props) {
  const [state, action, pending] = useActionState(saveSettings, initialFormState);
  const alertRef = useRef<HTMLDivElement>(null);
  const prod = settings.production;
  // A project admin may switch production off, but not on, and may not edit environments.
  const toggleLocked = !canManageProduction && !prod.actions_enabled;

  useEffect(() => {
    if (state.status !== "idle") alertRef.current?.focus();
  }, [state]);

  return (
    <form action={action} className="stack" data-testid="settings-form" key={version}>
      {state.status !== "idle" && (
        <div
          ref={alertRef}
          tabIndex={-1}
          role={state.status === "error" ? "alert" : "status"}
          className={`alert ${state.status}`}
          data-testid={`settings-${state.status}`}
        >
          {state.message}
          {state.code === "PRECONDITION_FAILED" && (
            <div>
              <a href={`/projects/${projectId}/settings`}>Reload the latest settings</a>
            </div>
          )}
        </div>
      )}
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="version" value={version} />

      <fieldset className="group">
        <legend>Process and writes</legend>
        <label>
          Process package
          <select name="process_package" defaultValue={settings.process_package}>
            <option value="sdlc@1">SDLC (sdlc@1)</option>
            <option value="stlc@1">STLC (stlc@1)</option>
          </select>
        </label>
        <label>
          Write mode
          <select name="write_mode" defaultValue={settings.write_mode}>
            <option value="read_only">read only - INPROOM never writes to external systems</option>
            <option value="draft_only">draft only - drafts stay inside INPROOM</option>
            <option value="approved_writes">approved writes - only after the required approvals</option>
          </select>
        </label>
        <label>
          Model policy
          <select name="model_policy" defaultValue={settings.model_policy}>
            <option value="tenant_default">tenant default</option>
            <option value="local_only">local models only</option>
            <option value="cloud_allowed">cloud models allowed</option>
          </select>
        </label>
      </fieldset>

      <fieldset className="group">
        <legend>Approvals</legend>
        <div className="checks" role="group" aria-label="Required approval scopes">
          {approvalScopes.map((s) => (
            <label key={s} className="check">
              <input
                type="checkbox"
                name="approvals.required_scopes"
                value={s}
                defaultChecked={settings.approvals.required_scopes.includes(s)}
              />
              {s}
            </label>
          ))}
        </div>
        <label>
          Minimum approvers
          <input name="approvals.min_approvers" type="number" min={1} max={3} defaultValue={settings.approvals.min_approvers} />
        </label>
        <label>
          Approval expiry (hours)
          <input name="approvals.expiry_hours" type="number" min={1} max={168} defaultValue={settings.approvals.expiry_hours} />
        </label>
        <p className="hint">Self-approval is never allowed.</p>
      </fieldset>

      <fieldset className="group">
        <legend>Sources</legend>
        <div className="checks" role="group" aria-label="Allowed source kinds">
          {sourceKinds.map((k) => (
            <label key={k} className="check">
              <input
                type="checkbox"
                name="sources.allowed_kinds"
                value={k}
                defaultChecked={settings.sources.allowed_kinds.includes(k)}
              />
              {k}
            </label>
          ))}
        </div>
        <label>
          Highest classification INPROOM may read
          <select name="sources.max_classification" defaultValue={settings.sources.max_classification}>
            <option value="public">public</option>
            <option value="internal">internal</option>
            <option value="confidential">confidential</option>
          </select>
        </label>
        <label className="check">
          <input type="checkbox" name="sources.approve_new_sources" defaultChecked={settings.sources.approve_new_sources} />
          New sources need approval before use
        </label>
      </fieldset>

      <fieldset className="group" data-testid="production-settings">
        <legend>Production actions</legend>
        <label className="check">
          <input
            type="checkbox"
            name="production.actions_enabled"
            defaultChecked={prod.actions_enabled}
            disabled={toggleLocked}
            aria-describedby="production-hint"
          />
          Allow production actions
        </label>
        {toggleLocked && prod.actions_enabled && <input type="hidden" name="production.actions_enabled" value="on" />}
        <label>
          Environments (comma-separated)
          <input
            name="production.environments"
            defaultValue={prod.environments.join(", ")}
            disabled={!canManageProduction}
            placeholder="prod, eu-prod"
          />
        </label>
        {!canManageProduction && (
          <input type="hidden" name="production.environments" value={prod.environments.join(",")} />
        )}
        <p className="hint" id="production-hint">
          {canManageProduction
            ? "Enabling production needs at least one environment and the release approval scope."
            : prod.actions_enabled
              ? "Only a tenant admin can change production settings. You can switch production actions off."
              : "Off. Only a tenant admin can enable production actions."}
        </p>
      </fieldset>

      <label>
        Change note
        <input name="change_note" maxLength={500} placeholder="Why are you changing this?" />
      </label>
      <button type="submit" className="primary" disabled={pending}>
        {pending ? "Saving…" : "Save settings"}
      </button>
    </form>
  );
}
