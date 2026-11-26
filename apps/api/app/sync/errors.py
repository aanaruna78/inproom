"""Plain-language explanations for connector and sync error codes (INPROOM-145 / story INPROOM-26).

APIs keep returning the stable `code`; screens and the attention/status APIs add `title`, `explanation` and
`action` from here. A unit test asserts that every code the connectors and the sync worker can emit is here.
"""

from typing import Any

CATALOG: dict[str, tuple[str, str, str]] = {
    # code: (title, what happened, what to do)
    "CREDENTIAL_INVALID": (
        "Reauthorization needed",
        "The provider rejected the stored token: it expired, was revoked, or its account lost access.",
        "Reauthorize the connection with a new token.",
    ),
    "REVOKED": (
        "Disconnected",
        "This connection was revoked in INPROOM and its token destroyed. Nothing syncs through it.",
        "Create a new connection and adopt this connection's sources.",
    ),
    "SOURCE_NOT_ACCESSIBLE": (
        "Source no longer accessible",
        "The token can no longer read this source.",
        "Grant the token access again, or remove the source.",
    ),
    "REPOSITORY_NOT_ACCESSIBLE": (
        "Repository not accessible",
        "The token can no longer read this repository; it is paused and out of the sync scope.",
        "Grant the token read access to the repository, then reauthorize or check the connection.",
    ),
    "BRANCH_NOT_FOUND": (
        "Branch missing",
        "A selected branch no longer exists; the repository is paused.",
        "Update the repository's branches on the Sources page.",
    ),
    "JIRA_PROJECT_NOT_ACCESSIBLE": (
        "Jira project not accessible",
        "The account can no longer browse this Jira project; it is paused.",
        "Restore Browse permission for the account, then check the connection.",
    ),
    "SPACE_NOT_ACCESSIBLE": (
        "Confluence space not accessible",
        "The account can no longer read this space; it is paused.",
        "Restore read access to the space, then check the connection.",
    ),
    "SPACE_CHANGED": (
        "Space key reused",
        "The space key now points at a different space, so the selection was not synced.",
        "Remove the space and select it again.",
    ),
    "PROVIDER_RATE_LIMITED": (
        "Provider rate limit",
        "The provider asked INPROOM to slow down; the sync will retry automatically.",
        "No action needed unless this persists; consider a less-shared token.",
    ),
    "PROVIDER_UNAVAILABLE": (
        "Provider unavailable",
        "The provider timed out or returned server errors; syncs retry with backoff.",
        "Check the provider's status page. Syncs resume automatically when it recovers.",
    ),
    "OBJECTSTORE_UNAVAILABLE": (
        "Content store unavailable",
        "INPROOM could not reach its object store to save or read source content; nothing partial was kept.",
        "Syncs retry automatically. If it persists, check the object store's health and credentials.",
    ),
    "BLOB_MISSING": (
        "Stored content missing",
        "A stored original is no longer in the object store (retention purge, or removed outside INPROOM).",
        "Run Reconcile now to fetch the current content again.",
    ),
    "BLOB_CORRUPT": (
        "Stored content failed its integrity check",
        "The bytes read from the object store do not match their recorded sha256, so they were not used.",
        "Run Reconcile now to fetch the content again, and check the object store for tampering or faults.",
    ),
    "UNKNOWN_PRODUCER": (
        "Unknown derivative producer",
        "A derivative names a producer version this INPROOM release does not have, so it cannot be rebuilt.",
        "Invalidate the item's derivatives so they are rebuilt with the current producers.",
    ),
    "NOT_STORED": (
        "Original not stored",
        "The revision's original bytes are not stored (over the size limit, or removed by retention).",
        "Nothing to rebuild from; a newer revision of the item is processed normally.",
    ),
    "PRODUCER_FAILED": (
        "Could not process content",
        "The content could not be turned into this derivative (for example, malformed text or JSON).",
        "Check the item in its source; it is retried when the item changes or is invalidated.",
    ),
    "PARSE_MALFORMED": (
        "Document could not be read",
        "The file is damaged, truncated or not what its type says (for example, not UTF-8, or not a PDF).",
        "Open the file in its own application; re-save or re-export it, then upload it again.",
    ),
    "PDF_ENCRYPTED": (
        "PDF is password-protected",
        "The PDF needs a password to open, so INPROOM cannot read its text.",
        "Remove the password (or export an unprotected copy) and upload it again.",
    ),
    "NO_TEXT_LAYER": (
        "No text in the document",
        "No page of the PDF has a text layer: it is probably a scan. OCR is not available yet.",
        "Upload a text-based PDF or the original document (for example, the DOCX).",
    ),
    "EMPTY_DOCUMENT": (
        "Document is empty",
        "The document has no text or tables to index.",
        "Check that the right file was uploaded or that the page has content.",
    ),
    "UNSUPPORTED_FORMAT": (
        "Format not supported",
        "INPROOM cannot parse this file type (supported: PDF, DOCX, Markdown, HTML, plain text).",
        "Convert the document to a supported format.",
    ),
    "PARSE_LIMIT_EXCEEDED": (
        "Document too large to parse",
        "The document exceeds a parsing limit (pages, sections, text size, or a compressed file that expands "
        "too much).",
        "Split the document into smaller parts.",
    ),
    "CODE_SYNTAX_ERROR": (
        "Source file does not parse",
        "The file has a syntax error at the line shown, so its symbols could not be extracted.",
        "Fix the syntax in the repository; the next commit is indexed automatically.",
    ),
    "LONG_LINES_NOT_SCANNED": (
        "Some lines were not scanned",
        "Very long lines (usually minified or generated code) were skipped when looking for symbols.",
        "Nothing to do for generated code; otherwise split the long lines.",
    ),
    "NEWER_REVISION": (
        "A newer version exists",
        "The cited revision is no longer the item's current one; the item changed after it was cited.",
        "Re-read the current revision before relying on the citation.",
    ),
    "PROVIDER_ERROR": (
        "Unexpected provider response",
        "The provider returned an unexpected error.",
        "Retry with Reconcile now; contact support if it persists.",
    ),
    "INTERNAL_ERROR": (
        "Sync error",
        "INPROOM hit an unexpected error while syncing; it retries automatically.",
        "Contact support with the job id if this persists.",
    ),
    "LEASE_EXPIRED": (
        "Worker stopped",
        "A sync worker stopped while running this job; the job was retried.",
        "No action needed.",
    ),
    "EXCESS_PRIVILEGE": (
        "More access than needed",
        "The token can write, but INPROOM only reads here.",
        "Replace it with a read-only token (least privilege).",
    ),
    "PLAN_CHANGED": (
        "Scan superseded",
        "The migration plan's source or scope changed while this scan was waiting or running.",
        "Run a new scan of the current plan, review it, then confirm.",
    ),
    "TOO_MANY_ISSUES": (
        "Jira project too large to inventory at once",
        "The migration scan stopped at 50,000 issues so it does not run unbounded.",
        "Set 'issues updated since' on the migration plan to narrow the history, then scan again.",
    ),
    "STALE": (
        "Data is stale",
        "No successful sync for more than 3 sync intervals.",
        "Use Reconcile now; if it fails, check the connection.",
    ),
    "SYNC_TRUNCATED": (
        "Too many pages",
        "The space has more pages than one sync reads, so part of it was not synced.",
        "Narrow the space with labels or page trees.",
    ),
    "NO_EPIC_TYPE": ("No epic type", "The Jira project has no epic-level issue type.", "No action needed."),
    "NO_STORY_POINTS_FIELD": (
        "No story points field",
        "No story points field was found in the Jira project.",
        "Map one on the Jira source page if the project uses estimates.",
    ),
    "NO_BOARD": (
        "No board",
        "The Jira project has no board, so sprints are unavailable.",
        "No action needed.",
    ),
    "OVERRIDE_TARGET_REMOVED": (
        "Mapped field removed",
        "A field override points at a Jira field that no longer exists.",
        "Update the mapping on the Jira source page.",
    ),
}


def explain(code: str | None) -> dict[str, Any] | None:
    if not code:
        return None
    title, explanation, action = CATALOG.get(
        code, ("Problem", "The connector reported an error.", "Check the connection or contact support.")
    )
    return {"code": code, "title": title, "explanation": explanation, "action": action}
