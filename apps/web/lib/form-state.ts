export type FormState = {
  status: "idle" | "error" | "success";
  message?: string;
  code?: string;
  /** Field the error relates to, so the input can be marked aria-invalid. */
  field?: string;
  /** Submitted values, echoed back so the form keeps the user's input after an error. */
  values?: Record<string, string>;
  traceId?: string;
};

export const initialFormState: FormState = { status: "idle" };
