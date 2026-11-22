# Configurable AI Process Lifecycle Platform - Mermaid Architecture Sources

Version 0.1 | 19 November 2026

The diagrams below are editable Mermaid sources. The companion PDF embeds visually rendered equivalents of these same graph structures.

## Reference architecture - process control plane

```mermaid
flowchart TB
 U[Business users / Process owners] --> UI[Process Studio and Control Center]
 UI --> API[API Gateway / Identity / Tenant Policy]
 API --> ORCH[Durable Process Orchestrator]
 ORCH --> AGENT[Agent Runtime and Skills]
 ORCH --> RULES[Policy / Approvals / Audit]
 AGENT --> KNOW[Knowledge Fabric and Retrieval]
 AGENT --> MODEL[Model Gateway]
 AGENT --> TOOLS[Secure Connector and Tool Gateway]
 MODEL --> FRONTIER[Frontier API Models]
 MODEL --> LOCAL[Local / Private LLMs]
 TOOLS --> SYSTEMS[Business Systems / ERP / Jira / Git / MES]
 KNOW --> DATA[Documents / Graph / Vector / SQL / Evidence]
```

## Configuration model - process as a product

```mermaid
flowchart LR
 T[Industry Process Template] --> D[Versioned Process Definition]
 D --> ST[Stages / State Machine]
 D --> CR[Roles / Rules / Approvals]
 D --> SK[Agent Skills / Prompts]
 D --> CT[Connector / Tool Bindings]
 D --> KP[Knowledge Sources / Policies]
 ST --> RUN[Process Instance]
 CR --> RUN
 SK --> RUN
 CT --> RUN
 KP --> RUN
 RUN --> EV[Decisions / Evidence / Outcomes]
```

## Document ingestion and governed learning

```mermaid
flowchart LR
 SRC[PDF / SOP / Wiki / Repo / ERP Data] --> PARSE[Extract / OCR / Parse]
 PARSE --> CHECK[Classify / ACL / Version / Trust]
 CHECK --> CHUNK[Chunk / Enrich / Extract Entities]
 CHUNK --> V[(Vector Index)]
 CHUNK --> G[(Relationship Graph)]
 CHECK --> OBJ[(Encrypted Source Store)]
 V --> RET[Hybrid Retrieval + Reranking]
 G --> RET
 OBJ --> RET
 RET --> CITE[Cited Context Package]
 CITE --> EVAL[Answer / Decision + Evaluation]
 EVAL --> FB[Reviewed Feedback]
 FB --> CHECK
```

## Policy-driven cloud and local model routing

```mermaid
flowchart TB
 REQ[Agent Request + Data Classification] --> POLICY{Tenant / Data / Task Policy}
 POLICY -->|Allowed external| CLOUD[Approved Frontier Provider]
 POLICY -->|Restricted / offline| LOCAL[Customer Local Model Endpoint]
 POLICY -->|Not allowed| DENY[Reject / Human Escalation]
 CLOUD --> VALID[Schema Validation + Evidence Check]
 LOCAL --> VALID
 VALID --> GATE{Tool Action?}
 GATE -->|No| ANSWER[Draft / Recommendation]
 GATE -->|Yes| APPROVAL[Policy Approval / Tool Proxy]
 APPROVAL --> ACTION[Logged Tool Execution]
```

## Human-governed process execution

```mermaid
stateDiagram-v2
 [*] --> Submitted
 Submitted --> ContextGathering
 ContextGathering --> Planning
 Planning --> Clarification: evidence missing
 Clarification --> Planning
 Planning --> Approval
 Approval --> Execution: approved
 Approval --> Rejected: rejected
 Execution --> Validation
 Validation --> Execution: fix/retry
 Validation --> Completed: pass
 Completed --> OutcomeReview
 OutcomeReview --> [*]
```

## Security boundaries and action enforcement

```mermaid
flowchart LR
 ID[Authenticated Tenant User] --> RBAC[Tenant / Project RBAC + ABAC]
 RBAC --> DATA[Permission-aware Retrieval]
 RBAC --> AGENT[Scoped Agent Execution]
 AGENT --> PROXY[Action Policy / Approval Proxy]
 PROXY --> VAULT[Credential Broker / Short-lived Token]
 PROXY --> API[External Tool API]
 DATA --> AUDIT[Append-only Audit + Evidence]
 AGENT --> AUDIT
 PROXY --> AUDIT
 API --> AUDIT
```

## SaaS, dedicated and on-prem deployment variants

```mermaid
flowchart TB
 CORE[Portable Application Stack + Common Contracts] --> SAAS[Multi-tenant SaaS]
 CORE --> DED[Dedicated Cloud / Customer VPC]
 CORE --> ON[On-prem / Air-gapped]
 SAAS --> M1[Approved Cloud Models]
 DED --> M2[Private Endpoint / Managed Models]
 ON --> M3[Local Inference + Local Vector DB]
 SAAS --> ISO1[Logical Tenant Isolation]
 DED --> ISO2[Dedicated Data Plane]
 ON --> ISO3[Customer-Owned Boundary]
```

## MVP delivery architecture and sequence

```mermaid
flowchart LR
 A[Foundation: Auth / Tenant / Config] --> B[Document Ingest / Evidence / RAG]
 B --> C[Process Template + State Machine]
 C --> D[Planner / Validator / Model Routing]
 D --> E[Human Approval + Read-only Connectors]
 E --> F[Pilot Evaluation + Audit]
 F --> G[Controlled Write Actions - Later]
```


## SDLC first process package — staged delivery

```mermaid
flowchart LR
 R[Story / Requirement] --> U[Evidence & Readiness]
 U --> I[Architecture and Code Impact]
 I --> P[Implementation + Test Plan]
 P --> A{Tech Lead Approval}
 A -->|Clarify| U
 A -->|Approved| S[Publish Plan / Optional Tool Action]
 S -.Later phase.-> C[Scoped Branch + PR]
 C --> Q[CI and Independent Review]
 Q --> L[Release Plan and Human Gate]
```

## STLC second process package — QA lifecycle

```mermaid
flowchart LR
 R[Requirement / SDLC Story] --> S[Test Scope and Strategy]
 S --> D[Test Case Design]
 D --> A{QA Approval}
 A -->|Revise| D
 A -->|Approved| X[Controlled Test Execution]
 X --> E[Test Evidence and Defects]
 E --> G[Regression and Coverage]
 G --> C[Closure / Release Readiness]
 E -.Defect findings.-> R
```

## Cross-package linkage — evidence traceability

```mermaid
flowchart TB
 REQ[Requirement Version] --> STORY[SDLC Story]
 STORY --> PLAN[Approved Implementation Plan]
 PLAN --> PR[PR and Build]
 REQ --> TEST[Test Case Version]
 PLAN --> TEST
 TEST --> EXEC[Test Execution Result]
 PR --> EXEC
 EXEC --> DEF[Defect Record]
 DEF --> STORY
 EXEC --> GATE[Release Readiness Evidence]
```

## Core data relationships

```mermaid
erDiagram
 TENANT ||--o{ PROJECT : owns
 PROJECT ||--o{ PROCESS_DEFINITION : defines
 PROCESS_DEFINITION ||--o{ PROCESS_INSTANCE : instantiates
 PROCESS_INSTANCE ||--o{ STAGE_RUN : contains
 PROCESS_INSTANCE ||--o{ ARTIFACT : creates
 ARTIFACT ||--o{ APPROVAL : governed_by
 DOCUMENT ||--o{ CHUNK : splits_into
 ARTIFACT ||--o{ EVIDENCE_REF : cites
 CHUNK ||--o{ EVIDENCE_REF : referenced_by
 STAGE_RUN ||--o{ TOOL_RECEIPT : emits
 STAGE_RUN ||--o{ MODEL_RECEIPT : emits
 PROJECT ||--o{ DOCUMENT : owns
 TENANT ||--o{ PROJECT : partitions
```

The figures embedded in the companion DOCX/PDF are rendered architecture visuals. These Mermaid sources are the editable canonical diagram definitions for later revisions, including the SDLC/STLC diagrams added in v1.
