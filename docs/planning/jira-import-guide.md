> **Publication update (22 November 2026):** All 14 epics and 65 stories were created in Jira and verified. This guide is retained solely as historical import guidance. **Do not re-import** its CSV files. See [live Jira key mapping](jira-key-map.md).

# INPROOM Jira Project — Import and Execution Guide

**Target:** https://aanaruna78.atlassian.net/jira/software/projects/INPROOM/boards/2

## Status
The proposed backlog is prepared but **not yet created in Jira**. Atlassian was authorized but Jira issue operations were not available for direct execution at this stage.

## Inventory
**14 Epics and 65 Stories**. The two CSVs use external planning IDs such as `E08` and `E08-S02`; these are **not Jira issue keys**. Do not present them as existing Jira tickets.

## Safe import sequence

1. In Jira, inspect whether the INPROOM project is team-managed or company-managed, available issue types, required fields, custom fields, and whether there are pre-existing issues. Reconcile by summary and scope to avoid duplicates.
2. Use `jira-import-epics.csv` to create only **Epic** issues. Map Issue Type, Summary, Description, Labels and Priority. `External Epic ID` is your tracking column; if Jira doesn't support an external ID field, retain it in the summary or in an import mapping table.
3. Export or record real Jira issue keys for each `E01`…`E14`. Create a mapping `External Epic ID → Jira Epic Key`.
4. In `jira-import-stories.csv`, replace or map the `Parent Epic External ID` column to the actual Jira parent key using the Jira importer's supported **Parent** or Epic association field, which differs by site/project/version.
5. Import **Story** issues, verifying a 2–3 item pilot import before the rest. Verify that parent links and descriptions, line breaks and acceptance criteria survive the import. Assign no fabricated owners or sprint numbers.
6. Check issue counts and browse at least one issue per epic. Do not blindly rerun an import: duplicate creation may occur.
7. Once stories are created, organize first milestone planning as below. Set actual sprint allocations and assignees in Jira after reviewing team capacity.

## Planned delivery order

**Iteration A — Foundation:** E01, E02, E03, E05 (minimum) and E13 authorization basics.
**Iteration B — Knowledge:** E04, E03 source revisions, E14 Knowledge Explorer.
**Iteration C — Approval-ready SDLC:** E06 + E07 + E08 and associated UX.
**Iteration D — Git evidence:** E09 and E10.
**Iteration E — QA lifecycle:** E11 and E12 with release/test evidence.
**Continuous:** E13 reliability/security/evals and E14 developer experience.

## Jira issue format

Each story includes:
- Objective and implementation plan
- Dependencies and expected results
- Four acceptance criteria
- Functional verification
- Cross-cutting tests (unit, integration, negative, permission and recovery)
- Definition of done

## Review gates

- Epics and stories must be approved before Jira writes; keep the approval record.
- **Must verify:** source ACL, approval gate, model/tool access and safe retries.
- For future product runtime, Git/CI/test results are evidence; generated text is not test success.
- Production deployment is forbidden by default.

## Post-import cross-reference

In GitHub, retain a mapping file once Jira keys exist (e.g. `docs/planning/jira-key-map.md`), and add links from implementation PRs to real issue keys. Never commit Jira API tokens or connector credentials.
