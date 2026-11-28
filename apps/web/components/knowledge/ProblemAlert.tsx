import type { Problem } from "@/lib/api";

/** An API problem, with its trace id so support can find the request (UX-04: actionable errors). */
export function ProblemAlert({ problem, what }: { problem: Problem; what: string }) {
  return (
    <div className="alert error" role="alert" data-testid="explorer-error">
      <strong>{what}:</strong> {problem.title}
      {problem.detail ? ` — ${problem.detail}` : ""}
      {problem.trace_id && <span className="hint"> (trace {problem.trace_id})</span>}
    </div>
  );
}
