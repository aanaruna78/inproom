# INPROOM Experience System — V2 (Premium Enterprise)

**Status:** Proposed design contract, not implemented or user-validated.  
**Owner:** E14 UX & Developer Experience.  
**Priority:** P0 product foundation.  
**Version:** 2.0 / 23 November 2026.

## The product experience decision

INPROOM is **not** an AI chatbot, a generic dashboard, or a clone of Jira. Its signature is a **work canvas** with human-editable plans, source-backed AI suggestions, and reversible, auditable publication.

**Visual direction:** Quiet editorial enterprise UX. Bright neutral canvas, ink-toned navigation, disciplined indigo accent, warm focus surfaces and minimal high-saturation color. Dense and precise where work happens; spacious and calm where decisions happen. Dark mode is a real token mode, not inverted screenshots.

**Benchmark inputs (not endorsements or copied UI):**
- Linear 2026 refresh: consistent headers, recognizable controls, content-first reduced visual friction — https://linear.app/changelog/2026-03-12-ui-refresh
- Vercel 2026 dashboard redesign: frequent-workflow-first navigation — https://vercel.com/changelog/dashboard-navigation-redesign-rollout
- Atlassian Design System: semantic tokens, accessibility, scalable interaction patterns — https://atlassian.design/get-started/about-atlassian-design-system
- Figma variables and modes: maintainable design tokens and prototype modes — https://help.figma.com/hc/en-us/articles/14506821864087-Overview-of-variables-collections-and-modes

These references inform principles only; build an original INPROOM identity.

## Design principles (enforceable)

1. **Work before widgets.** Show the next decision and editable object, not a wall of KPI cards.
2. **One primary action per context.** Secondary actions remain adjacent but quieter.
3. **AI is a colleague, not a magician.** Provenance, input assumptions, confidence and preview appear on the same surface.
4. **Local edits are sacred.** AI regeneration shows a selectable diff; never silently replaces human text.
5. **Progress is a state machine.** Draft → Proposed → Needs review → Approved → Publishing → Synced / Conflict / Failed.
6. **New and Migration are meaningfully different.** New proposes first backlog; Migration imports existing keys and prioritizes reuse.
7. **Everything is traceable.** Document → requirement → Epic → Story → Subtask → PR → tests → release evidence.
8. **Polish is interaction quality.** Perceived speed, keyboard navigation, subtle feedback, excellent empty/error/loading states.
9. **Accessibility isn't decoration.** WCAG 2.2 AA goal, including treegrid semantics, focus retention and announcements.
10. **Deterministic proof outranks AI prose.** Verification states are backed by test runner/CI evidence.

## Information architecture (V2)

Global rail (maximum 6 destinations): **Home, Projects, Inbox, Agents, Reports, Admin**.
Inside project workspace: **Overview, Requirements, Knowledge, Plan, Build, Test, Release**.
The navigation supports collapse/resize and persists per user; project selection is sticky.
Global command bar offers search and commands via Cmd/Ctrl+K; saved views and context-aware quick creation.
Do not show separate navigation items for every connector in the global sidebar; group integrations under Settings.

## Core page pattern

**Shell**: 224–248px resizable sidebar, 56px top command bar, 52px contextual page bar, 12-column adaptive work surface.
**Work canvas**: For Requirements Studio: 264px source panel (collapsible), flexible 620px+ tree-grid, 360–440px inspector (collapsible). At <1280px inspector becomes overlay drawer; at <900px source panel becomes collapsible sheet. On mobile prioritize read/review, not dense authoring.
**Hierarchy**: Project → Epic → Story → Subtask; typed icon and indent, origin (AI/User/Imported), sync state, linked Jira key, owner, updated and context action. Avoid rainbow badges and redundant chips.
**Detail inspector**: Overview, Plan, Acceptance, Tests, Sources, Diff, Activity; maintain per-node tab selection.
**Approval UX**: fixed bottom action rail on edit/proposal with exact create/update/reuse counts, remote target, revision hash and independent approve/publish stages.
**Knowledge UX**: source reading and cited answers in the same panel, not a floating chat bubble detached from evidence.
**Agent UX**: explain task goal, inputs, expected artifacts, tools, budget, progress milestones and the stop/approve/retry actions.

## Design tokens (reference light mode)

| Category | Values |
|---|---|
| Canvas | #F7F8FA |
| Surface | #FFFFFF |
| Elevated | #FCFCFE |
| Sidebar | #101828 |
| Primary text | #172033 |
| Secondary text | #667085 |
| Hairline border | #E7E9EF |
| Brand primary | #5546E8 |
| Brand hover | #4033CC |
| Subtle accent | #EFEDFF |
| Success | #067647 |
| Caution | #B54708 |
| Critical | #B42318 |
| Focus ring | 2px #5546E8 with 2px outer offset |
| Radius | 6 controls / 10 panels / 14 modals |
| Type | Inter or system UI, 13–14 body, 12 table meta, 20/24 page title, tabular numbers |
| Density | Compact 36px row; Comfortable 44px row (user setting) |
| Motion | 120–180ms small transitions; reduced-motion uses instant |

Dark-mode tokens must be semantic, contrast-tested and implemented as token aliases rather than inverted CSS.

## Critical interactions

### Requirement-to-Jira
1. Choose project; mode NEW or MIGRATION is visibly persistent.
2. Add sources (files/text), with processing and provenance status.
3. Ask AI to draft or manually add Epic/Story; system offers explicit generation scope.
4. Tree displays AI/User/Imported origin. User reviews node; detail inspector shows plan, acceptance, tests and evidence.
5. AI diff review is field-by-field. User can accept one section without accepting all; user text never auto-overwritten.
6. For MIGRATION, matched Jira issue receives clear **reuse / update / new** disposition and confidence explanation.
7. Bulk review previews exactly how many objects are created/updated/reused and destination project.
8. Approve version, then publish; partial failures show per-item receipt with retry. Never show misleading all-green success.

### Overview page
Exception-first work queue: approvals, blockers, stale source connections, active agent runs, current stage. Small relevant metrics only; avoid default charts without meaningful data. Empty state guides first project connector and first requirement.

### Knowledge
Split Search / Sources / Relationships; show freshness, cite anchors, graph/table alternative, ACL-safe no-results. Design clear "cannot access" state.

### Build and Test
Review acceptance-criterion evidence in rows: criterion → PR diff → CI test/check → verdict with uncertainty. QA surfaces runnable test data and defects, not only percentage donuts.

## Components to implement

AppShell, ProjectSwitcher, CommandPalette, ContextHeader, SegmentedView, WorkTreeGrid (virtualized + accessible), SourceUploader, SourceEvidenceCard, OriginMarker, ScopeBadge, InspectorTabs, AIProposalDiff, ApprovalBar, SyncReceipt, AgentRunTimeline, TraceabilityRow, TestEvidenceMatrix, Toast/InlineNotice, ConfirmDialog, EmptyState, Skeleton, ErrorBoundary, KeyboardShortcutsHelp, ThemeDensitySettings.

## State and content guidance

Each component must have: loading, empty, ready, pending, optimistic, stale, conflict, denied and error states when applicable. Wording should be specific: **“2 Jira stories couldn't be created; 8 were published”**, not **“Something went wrong.”**
Status labels must never rely on color alone. Success is only after returned write receipts or actual test result.

## Test and usability gates — proposed targets (must be baselined)

- Scenario tests with at least **6 representative personas** and **8 end-to-end tasks** across NEW/MIGRATION.
- Aim for ≥90% task completion without moderator help on priority flows; benchmark before committing release gate.
- No unintended Jira write in testing; 100% external writes show destination and exact changed-item preview.
- Keyboard-only full flow for create/edit/navigate/tree review; axe and manual WCAG 2.2 AA audit with no critical/high accessibility defects.
- Virtualized tree should handle 2,000 work items without blocking input, tested on representative hardware; actual performance budgets to be baselined.
- Run Lighthouse on representative production build and monitor interaction latency, layout shifts and per-route web vitals. No invented performance claims.
- Visual regression for 1280, 1440, 1920 and tablet widths, light/dark, compact/comfortable, empty/loading/error/long-content states.
- Source/project ACL, read-only role, stale approval, partial sync and concurrency test coverage.
- Before claiming “industry-leading”, validate independently with 5+ external target users; project aspiration is not an achieved quality rating.

## Artifacts
- [Screen V2 SVG](visuals-v2/01-requirements-studio.svg)
- [Project command center V2](visuals-v2/02-project-command-center.svg)
- [Component token JSON](tokens-v2.json)
- [Legacy baseline](README.md)
- [Jira UX Epic](https://aanaruna78.atlassian.net/browse/INPROOM-17)
