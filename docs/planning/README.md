# Jira INPROOM backlog — publication status

**Prepared:** 2026-11-22  
**Target Jira project:** `INPROOM` at `aanaruna78.atlassian.net`  
**State:** **Planned / not created in Jira by this publication step.**

This backlog originates from INPROOM's architecture, complete lifecycle and UX documents. The Atlassian connection was authorized, but direct Jira issue operations were not exposed in this execution environment, so no Jira write was attempted.

## Content inventory
- 14 epics
- 65 developer-ready stories
- Each story includes implementation work, expected result, functional verification and error/boundary tests.
- Published source files: [epic CSV](jira-import-epics.csv), [story CSV](jira-import-stories.csv), [full backlog](implementation-backlog.md), [import guide](jira-import-guide.md).

## Issue creation rule
**Do not import twice.** Before creation, inspect the actual Jira project, issue type IDs, workflow, existing keys and custom required fields, then reconcile to prevent duplication. Jira Cloud's CSV mapping and parent hierarchy are project-configuration-specific. Create epics first, record their Jira keys, map each story to its actual parent key, and then create the stories with approved details.

## Release and acceptance philosophy
Agent opinions cannot replace deterministic test evidence. Approval records bind to the exact proposal. Every external write is scoped, idempotent and auditable. Automated production deployment is disabled by default.
