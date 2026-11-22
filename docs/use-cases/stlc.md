# INPROOM Test — STLC Use Cases

## UC-QA-01 Generate QA work from a release candidate
Actor: QA lead. Input: signed-off release candidate, its stories/requirements, changed components and historical test evidence. Agent drafts QA stories, test scenarios and regression mapping; QA lead approves the Jira publish/allocation. Output: linked QA work items and traceability edges.

## UC-QA-02 Generate test plans and test cases
From acceptance criteria and design constraints, draft positive, negative, boundary, integration, accessibility, performance and security scenarios as relevant. Every proposed case links to a requirement/criterion and prerequisite data/environment. Missing requirements are escalated; the model must not invent business rules.

## UC-QA-03 Manual verification
Tester records environment, build hash, expected/actual outcomes, attachments, steps and per-criterion results. QA closure requires configured evidence coverage and authorized sign-off.

## UC-QA-04 Automation harness
An isolated runner executes selected frameworks and existing test suites using controlled credentials, timeout/quotas and sanitized artifacts. AI can recommend tests, generate draft scripts or triage failures; actual results come from the test runner, not generated prose. Support reruns with flaky-test annotation and quarantine.

## UC-QA-05 Defect creation and retest
A failed criterion creates or links a defect to original requirement, story, build and test execution. Capture reproducible steps, severity proposal, evidence and owner. New fix PR is verified, affected regression scope refreshed and retest performed. AI cannot silently close defects.

## UC-QA-06 Release QA sign-off
Build a requirement → test → execution → defect → sign-off matrix. Block closure when critical evidence is missing or outstanding defects breach policy. Generate an auditable exception record when explicitly approved.

## QA metrics
Requirement coverage, test execution completion, pass rate, escaped defects, flaky-test ratio, cycle time, coverage precision and evidence completeness. All metrics defined by actual linked evidence.
