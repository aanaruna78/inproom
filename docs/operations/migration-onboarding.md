# Migration onboarding (INPROOM-19)

Onboard an existing product whose work already lives in Jira. INPROOM **inventories** the Jira project and keeps
Jira **authoritative**: original keys, statuses, authors and the Epic → Story → Sub-task hierarchy stay exactly as
they are. Nothing is re-created, re-numbered or written to Jira.

## Steps
1. **Create the project in MIGRATION mode.** Workspace → New project → type MIGRATION. It starts as `PENDING_SOURCES`.
2. **Select the Jira project** on the project's Sources page. This needs a Jira connection; see the connector docs.
3. **Plan** (project → Migration → *1. Source of truth*):
   - choose that Jira project;
   - set the history scope: full, or only issues updated since a date;
   - only a project admin can change the plan.
4. **Scan** (*2. Scan*): the sync worker (`make worker`) pages through every issue.
   - Progress shows the pages and issues read, and the account the scan ran as.
   - If the worker stops mid-scan, the job resumes from the last saved page. Re-scans update issues in place and never duplicate them.
5. **Preview** (*3. Preview*):
   - counts by type;
   - the hierarchy, where you can drill into children; renamed keys show "was …";
   - the **diagnostics**, explained below.
6. **Confirm baseline** (*4. Baseline*):
   - records the reviewed inventory as the baseline and moves the project to **ACTIVE**;
   - needs a completed scan that is under 24 hours old;
   - after this, the plan cannot change.
7. **Afterwards:** *Re-sync* any time. **Drift** lists what was added, changed (key, type, status, parent, summary) or removed since the baseline. The baseline itself never moves.

## Diagnostics: reported, never invented
| Flag | Meaning | What INPROOM does |
|---|---|---|
| `orphan` | The parent isn't in the inventory: deleted, moved, or not visible to the scanning account | Keeps the parent reference as Jira gave it; never re-parents |
| `unsupported_type` | A type outside epic / story / task / sub-task / bug (for example Spike) | Keeps it, typed `other` with the raw type name |
| `unresolved_link` | A link to an issue outside the project, or one not visible | Records the target key; never follows it |
| `key_changed` | The key changed since the issue was first seen (moved, or the project was re-keyed) | Keeps `original_key` |
| `duplicate_key` | Two different issues carry the same key | Reports both |

Issues are identified by **Jira issue id**. An issue missing from a **completed full-history** scan is marked removed, and its history is kept. Interrupted scans, and scans limited to "updated since", never remove anything.

## Write-readiness
The migration page, and `GET /v1/projects/{id}/migration` (`write_readiness`), say whether automated Jira changes would be safe. Reasons it may not be safe:

| Reason | Meaning |
|---|---|
| `NO_BASELINE` | No baseline has been confirmed |
| `SCAN_REQUIRED` | No scan has run yet |
| `SCAN_FAILED` | The last scan failed |
| `SCAN_IN_PROGRESS` | A scan is running |
| `SCAN_STALE` | The last scan is more than 24 hours old |
| `SOURCE_PAUSED` | The Jira source is paused |
| `PERMISSION_LOST` | The account lost access |

INPROOM-19 never writes to Jira. Publishing (E07-S04) must refuse to write unless `ready` is true.

## Limits and what comes later
- A scan stops at 50,000 issues (`TOO_MANY_ISSUES`). For larger projects, narrow the plan with "issues updated since".
- Matching new requirements to these existing issues, and proposing reuse/update/create, is **E07** (INPROOM-97/98).
- Approving those proposals is **E06-S03**. Publishing them is **E07-S04**.
