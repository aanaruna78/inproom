# AI Process Lifecycle Platform — Master Architecture Mermaid Sources v2.0

## High-level platform
```mermaid
flowchart LR
 User[Project workspace] --> API[API Gateway and IAM]
 API --> WF[Durable Workflow Engine]
 WF --> AG[Agent Runtime]
 WF --> POL[Policy and Audit]
 WF --> CON[Git Jira Confluence Connectors]
 AG --> MOD[Model Gateway: cloud BYOK local]
 AG --> KN[Knowledge Fabric]
 AG --> RUN[Dev and QA Harnesses]
 KN --> DATA[(PostgreSQL pgvector graph object store Markdown)]
```

## Lifecycle
```mermaid
flowchart TD
 A[New or migration project] --> B[Connect Git Jira Confluence]
 B --> C[Ingest and build graph vector Markdown]
 C --> D[Requirement intake]
 D --> E[AI epics and stories]
 E --> F{Approve exact proposal?}
 F -->|No| E
 F -->|Yes| G[Create Jira issues]
 G --> H[Sprint analysis and plan]
 H --> I[Approved Mermaid and Confluence docs]
 I --> J[Human or AI-assisted development]
 J --> K[Commit PR and independent verification]
 K --> L{Quality gates pass?}
 L -->|No| J
 L -->|Yes| M[Release candidate]
 M --> N[Generate QA work and obtain approval]
 N --> O[QA sprint and test execution]
 O --> P{Acceptance evidence passes?}
 P -->|No| Q[Defect and development loop]
 Q --> H
 P -->|Yes| R[QA signoff release closure]
 R --> C
```

## Knowledge
```mermaid
flowchart LR
 SRC[Sources] --> VER[Immutable versioned snapshot ACL]
 VER --> PARSE[Parse chunk symbol entity]
 PARSE --> VECTOR[(Vector index)]
 PARSE --> GRAPH[(Relationship graph)]
 PARSE --> MD[(Versioned Markdown export)]
 VECTOR --> RET[ACL-scoped hybrid retrieval]
 GRAPH --> RET
 MD --> RET
 RET --> AG[Grounded agents with citations]
```

## Models and harnesses
```mermaid
flowchart TB
 AG[Agents] --> MG[Policy model gateway]
 MG --> CL[Cloud model APIs]
 MG --> BYOK[Encrypted customer-provided API credentials]
 MG --> LOCAL[Local private LLM]
 AG --> DEV[Isolated Dev Harness]
 AG --> QA[QA Automation Harness]
 DEV --> CI[Build tests static analysis PR evidence]
 QA --> TEST[API UI integration execution evidence]
```

## Security and approvals
```mermaid
flowchart LR
 U[Authenticated actor] --> A[Project and tenant authorization]
 A --> P[Policy engine]
 P --> H{Human approval when required}
 H -->|Approved exact hash| BROKER[Short lived credential broker]
 BROKER --> TOOL[Authorized connector or sandbox]
 TOOL --> AUDIT[(Append-only audit)]
 P --> AUDIT
```

## SaaS / on-prem deployment
```mermaid
flowchart LR
 USER[Client + SSO] --> CP[SaaS Control Plane]
 CP --> STORE[(Tenant-scoped storage)]
 CP --> BROKER[Secure Connector Broker]
 BROKER --> ENTERPRISE[Customer Git Jira Confluence CI]
 BROKER --> LOCAL[Optional local model + harness]
 CP -. alternative .-> PRIVATE[Dedicated VPC / On-Prem Plane]
```

## QA handoff
```mermaid
sequenceDiagram
 participant RM as Release Manager
 participant WF as Workflow
 participant QA as QA Agent
 participant TL as QA Lead
 participant J as Jira
 participant H as QA Harness
 RM->>WF: Approve immutable candidate
 WF->>QA: Generate test and QA story proposal
 QA-->>TL: Review coverage, scope, allocation
 TL->>WF: Approve proposal hash
 WF->>J: Create QA stories idempotently
 J-->>WF: Issue IDs
 WF->>H: Run authorized tests
 H-->>WF: Immutable results and artifacts
 WF-->>TL: Coverage, defects, signoff decision
```

## PR verification
```mermaid
sequenceDiagram
 participant D as Developer
 participant G as Git
 participant W as Workflow
 participant V as Verification Agent
 participant CI as CI Test Tools
 participant R as Reviewer
 D->>G: Push commit and open PR
 G->>W: Webhook with PR SHA
 W->>V: Verify against approved Jira criteria
 V->>CI: Fetch executed checks and evidence
 CI-->>V: Results, coverage, scan reports
 V-->>R: Criterion matrix and uncertainty
 R->>W: Gate decision / request changes
 W->>G: Only authorized comments or checks
```

