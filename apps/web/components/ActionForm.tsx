"use client";

import { useActionState, useEffect, useRef, useState, type ReactNode } from "react";

import { initialFormState, type FormState } from "@/lib/form-state";

export type Field = {
  name: string;
  label: string;
  hint?: string;
  required?: boolean;
  pattern?: string;
  maxLength?: number;
  minLength?: number;
  options?: { value: string; label: string }[];
  defaultValue?: string;
  /** "password" fields are never re-filled after an error (secrets are not echoed back). */
  type?: "text" | "password" | "checkbox" | "url";
  placeholder?: string;
};

type Props = {
  action: (prev: FormState, form: FormData) => Promise<FormState>;
  fields: Field[];
  submitLabel: string;
  hidden?: Record<string, string>;
  testId: string;
  children?: ReactNode;
};

/**
 * Accessible create form: keeps user input on errors, focuses the error summary, marks the related
 * field aria-invalid, and sends one Idempotency-Key per form instance so double-submits are safe.
 */
export function ActionForm({ action, fields, submitLabel, hidden, testId }: Props) {
  const [state, formAction, pending] = useActionState(action, initialFormState);
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [formKey, setFormKey] = useState(0);
  const summaryRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (state.status === "error") summaryRef.current?.focus();
    if (state.status === "success") {
      setIdempotencyKey(crypto.randomUUID());
      setFormKey((k) => k + 1); // reset inputs after a successful create
    }
  }, [state]);

  return (
    <form action={formAction} className="stack" data-testid={testId} key={formKey} noValidate={false}>
      {state.status !== "idle" && (
        <div
          ref={summaryRef}
          tabIndex={-1}
          role={state.status === "error" ? "alert" : "status"}
          className={`alert ${state.status}`}
          data-testid={`${testId}-${state.status}`}
        >
          {state.message}
          {state.traceId && <div className="hint">Reference: {state.traceId}</div>}
        </div>
      )}
      <input type="hidden" name="idempotency_key" value={idempotencyKey} />
      {Object.entries(hidden ?? {}).map(([k, v]) => (
        <input key={k} type="hidden" name={k} value={v} />
      ))}
      {fields.map((f) => {
        const invalid = state.status === "error" && state.field === f.name;
        const value = state.status === "error" ? state.values?.[f.name] : undefined;
        const describedBy = f.hint ? `${testId}-${f.name}-hint` : undefined;
        if (f.type === "checkbox") {
          return (
            <label key={f.name} className="check">
              <input type="checkbox" name={f.name} defaultChecked={f.defaultValue === "on"} />
              {f.label}
              {f.hint && <span className="hint"> {f.hint}</span>}
            </label>
          );
        }
        return (
          <label key={f.name}>
            {f.label}
            {f.options ? (
              <select name={f.name} defaultValue={value ?? f.defaultValue}>
                {f.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                name={f.name}
                type={f.type ?? "text"}
                placeholder={f.placeholder}
                autoComplete={f.type === "password" ? "off" : undefined}
                required={f.required}
                pattern={f.pattern}
                maxLength={f.maxLength}
                minLength={f.minLength}
                defaultValue={f.type === "password" ? undefined : (value ?? f.defaultValue)}
                aria-invalid={invalid || undefined}
                aria-describedby={describedBy}
              />
            )}
            {f.hint && (
              <span className="hint" id={describedBy}>
                {f.hint}
              </span>
            )}
          </label>
        );
      })}
      <button type="submit" className="primary" disabled={pending}>
        {pending ? "Saving…" : submitLabel}
      </button>
    </form>
  );
}
