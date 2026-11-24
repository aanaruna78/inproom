# Source sync worker (INPROOM-25)

The worker keeps every selected source (GitHub repositories, Jira projects and Confluence spaces) in step with the provider. It also turns webhooks into safe reconcile jobs.

## Run it

```bash
make worker                                   # = cd apps/api && uv run python -m app.worker
uv run python -m app.worker --once            # process at most one job, then exit
uv run python -m app.worker --tenant <uuid>   # only that tenant's jobs (drain or debug one tenant)
```

It stops cleanly on SIGTERM or SIGINT, after finishing the current job. Running several workers is safe: jobs are claimed with a lease using `FOR UPDATE SKIP LOCKED`.

| Setting | Default | Meaning |
|---|---|---|
| `INPROOM_SYNC_INTERVAL_SECONDS` | 900 | Incremental reconcile interval per source |
| `INPROOM_SYNC_FULL_INTERVAL_SECONDS` | 86400 | Full reconcile: Jira deletions and schema; GitHub repository access |
| `INPROOM_SYNC_LEASE_SECONDS` | 300 | A job whose worker stops responding for this long is recovered |
| `INPROOM_PUBLIC_URL` | unset | Base URL shown in webhook setup (receivers live at `/v1/hooks/{github,jira,confluence}/{tenant}/{connection}`) |

## What happens to a change

1. A webhook, the schedule, or **Reconcile now** queues a job. There is at most one queued job per source; extra triggers join it.
2. The worker claims it and reads the provider's current state.
3. It compares that state with the source's watermark and emits outbox events only for real changes. Events, watermark and job completion commit **in one transaction**, so duplicate, out-of-order and missed webhooks, retries and crashes never produce a duplicate or lost event.

## Job states and what to do

| Status | Meaning | Action |
|---|---|---|
| QUEUED | Waiting (possibly until `run_after`, after a rate limit or backoff) | none |
| RUNNING | Leased by a worker | If the worker died, the lease expires and the job is re-run (`reason: recovery`) |
| SUCCEEDED | Done; see `result` for counts | none |
| SKIPPED | Source deselected or paused, project archived, or kind no longer allowed | none |
| FAILED | Retrying cannot help: credential invalid (401), access lost, space key reused | Fix the connection (Integrations → Check now / Rotate token), then **Reconcile now** |
| DEAD | 6 attempts failed (5xx or timeouts); audited as `source.sync.dead` | Check the provider's status, then **Reconcile now**. The scheduler also retries on the next interval |

Rate limits (429) wait for the provider's Retry-After and don't count as attempts. Other transient errors back off for 30 s × 2ⁿ, up to 30 minutes.

## Where to look

- **Sources → Sync status:** fresh, stale (no success for 3 intervals) or failing; last error; queued or running.
- **`GET /v1/projects/{id}/sources/jobs`:** recent jobs, with reason, attempts, result and error code.
- **Worker logs:** one JSON line per job (`"event": "sync.job"`), with job id, kind, reason, mode, attempt, duration, outcome and error code. Logs never contain credentials or webhook secrets.

## Webhooks

- **GitHub** webhooks are registered by INPROOM when a repository is selected with "Register a webhook".
- **Jira and Confluence:** on Integrations, use **Webhook setup → Generate webhook secret**. Copy the URL and secret into the webhook settings of the Jira or Confluence administration. The secret is shown only once, and generating a new one replaces the old.
- Deliveries are checked with HMAC-SHA256 (`X-Hub-Signature`) and deduplicated by delivery id. They only queue jobs.

## Connection health and recovery (INPROOM-26)

- **Automatic health checks:** the worker checks every connection every `INPROOM_HEALTH_INTERVAL_SECONDS` (default 3600). A scheduled check is audited only when the status or issues change.
- **Invalid credential:** if any sync gets "unauthorized" (401), the connection becomes **INVALID** immediately (audit `connection.invalidated`) and no further syncs run for it. It appears under **Integrations → Needs attention** with a plain-language reason and action.

**Why a source is paused** (`pause_cause`) and who can resume it:

| Cause | Set by | Resumed by |
|---|---|---|
| `access` | Health check (lost access, missing branch) | Automatically: a health check or reauthorization that finds it readable again |
| `excluded` | A project admin (**Exclude**) | Only a project admin (**Include again**). Health checks, re-selecting and syncs never resume it |
| `connection_revoked` | Disconnect (revoke) | Only by moving the source to a new connection (**Move sources** / `POST /v1/connections/{id}/adopt`) |

**Freshness** (sync scope, Sources, Jira and Confluence pages) is one of:

| Value | Meaning |
|---|---|
| `fresh` | Synced recently |
| `stale` | No success for 3× the sync interval |
| `failing` | Recent syncs failed |
| `never` | Never synced |
| `paused` | Paused (see cause above) |
| `excluded` | Excluded by a project admin |

`data_as_of` is the time of the last successful sync. Pages for stale or paused sources show a "data as of …" banner.

**Fixing a broken token:** open the connection's page and use **Reauthorize** with a new token. INPROOM validates the token, checks every source, resumes those that were paused only for access, and queues a sync for each. The result lists what resumed and what is still blocked, and why.

**Disconnecting:** the connection page shows what would stop (projects, sources, webhooks) before you confirm. Revoking destroys the token, skips queued jobs and makes its webhooks return 404. History is kept.

**Reconnecting after a disconnect:**
1. Create a new connection to the **same provider and site**.
2. On the old connection's page, use **Move sources**.
3. Each source is re-verified with the new token. Verified sources resume and sync. The others stay paused, with the reason shown. Excluded sources keep their exclusion.
4. Webhooks are not carried over: re-register GitHub hooks, or generate a new Jira or Confluence webhook secret.
