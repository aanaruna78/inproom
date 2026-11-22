# INPROOM — Local Development Setup

Everything runs from the repository root through `make`. Run `make help` to list all commands.

## 1. Prerequisites

| Tool | Version | Install |
|---|---|---|
| Docker Engine + Compose v2 | recent | https://docs.docker.com/engine/install/ |
| uv (Python toolchain; installs Python 3.12 itself) | ≥ 0.5 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js + npm | Node ≥ 20 (22 LTS recommended) | https://nodejs.org/ |

Check them:

```bash
make doctor
```

`doctor` names each missing tool with the fix and exits non-zero. Port warnings are expected if the stack is already running.

## 2. First run

```bash
make setup     # creates .env from .env.example, installs API (uv) and web (npm) dependencies
make up        # starts PostgreSQL+pgvector, Keycloak and the S3 object store; waits until healthy
make migrate   # applies Alembic migrations (baseline enables pgvector + pgcrypto)
make test      # API unit + integration tests, OpenAPI contract check, web typecheck
```

All four must succeed on a clean machine. Then run the apps in two terminals:

```bash
make api       # http://localhost:8000  (docs at /docs, /healthz, /readyz)
make web       # http://localhost:3000  (shows live API status)
```

## 3. Local services

| Service | URL / port | Local credentials |
|---|---|---|
| PostgreSQL 16 + pgvector | localhost:55432, db `inproom` | inproom / inproom |
| Keycloak 26 (realm `inproom`) | http://localhost:18080 | admin / admin |
| S3-compatible object store (SeaweedFS) | http://localhost:18333 | anonymous (local only) |
| API | http://localhost:8000 | — |
| Web | http://localhost:3000 | — |

These are **local-only** defaults from `.env.example`. Change ports in `.env` (git-ignored). Never reuse them in shared environments.

> MinIO is listed in the architecture as the reference S3 store; its images are no longer freely published on Docker Hub, so local development uses SeaweedFS's S3 gateway. Code must depend only on the S3 API so either works.

## 4. Everyday commands

| Command | Purpose |
|---|---|
| `make lint` / `make format` | Ruff lint + format check / auto-fix; web typecheck |
| `make contracts` | Regenerate `packages/contracts/openapi.json` after API changes — commit the result |
| `make contracts-check` | Fails if the committed contract is stale (also enforced in CI) |
| `make down` / `make reset` | Stop the stack / stop and delete its data |

## 5. Repository layout

```
apps/api            FastAPI service (app/, migrations/, tests/, scripts/)
apps/web            Next.js control center
packages/contracts  Generated OpenAPI contract (source of truth for clients)
infra/local         Docker Compose stack and Keycloak realm
scripts/            doctor.sh and other developer tooling
.github/workflows   CI: API lint/migrate/test/contract + web typecheck/build
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `make up` fails with "port is already allocated" | Change the port in `.env`, then `make up` again |
| `/readyz` returns 503 `pgvector: extension not installed` | Run `make migrate` |
| `/readyz` returns 503 `database: unreachable` | `make up`; check `docker compose -f infra/local/docker-compose.yml ps` |
| Integration test is skipped | `INPROOM_TEST_DATABASE_URL` unset — run through `make test` so `.env` is loaded |
