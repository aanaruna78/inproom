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
