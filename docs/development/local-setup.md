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
| S3-compatible object store (SeaweedFS) | http://localhost:18333 | anonymous (local only); holds synced source content, see [content-store.md](../operations/content-store.md) |
| API | http://localhost:8000 | — |
| Web | http://localhost:3000 | — |

### Local demo users (Keycloak realm `inproom`)

| User | Password | Tenant (`tid` claim) | Tenant role (token) |
|---|---|---|---|
| alice | alice-local | `a0000000-0000-4000-8000-00000000000a` (tenant A) | tenant_admin |
| dave | dave-local | `a0000000-0000-4000-8000-00000000000a` (tenant A) | — (plain member) |
| erin | erin-local | `a0000000-0000-4000-8000-00000000000a` (tenant A) | — (plain member) |
| bob | bob-local | `b0000000-0000-4000-8000-00000000000b` (tenant B) | tenant_admin |

Project roles (project_admin, developer, viewer, ...) are **not** in the token: a project admin grants them on
the project's **Members** screen (or `PUT /v1/projects/{id}/members/{user_id}`), and they take effect on the
next request. A user can be added only after signing in once (that registers them in the tenant directory).
See [the role matrix](../security/rbac-matrix.md).

> Existing local stacks: Keycloak imports the realm only when it is first created. To pick up dave's
> changed role and the new user erin, run `make reset && make up && make migrate` (deletes local data).

Get a token for API calls (client `inproom-dev-cli` allows the password grant **locally only**):

```bash
TOKEN=$(curl -s -d grant_type=password -d client_id=inproom-dev-cli -d username=alice -d password=alice-local \
  http://localhost:18080/realms/inproom/protocol/openid-connect/token | jq -r .access_token)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/me
```

The realm is imported only when Keycloak starts without existing data. After changing `infra/local/keycloak/inproom-realm.json` run `docker compose --env-file .env -f infra/local/docker-compose.yml up -d --force-recreate keycloak`.

### Web sign-in and end-to-end tests

`make api` + `make web`, then open http://localhost:3000/workspaces — you are redirected to Keycloak (Authorization Code + PKCE, client `inproom-web`). Sign in as one of the demo users above. The web server keeps the API access token in its encrypted session cookie and never exposes it to browser JavaScript.

```bash
make e2e      # Playwright, headless Google Chrome; starts the API and a production web build if not running
```

Web settings (in `.env`): `AUTH_SECRET` (local value only — generate one with `npx auth secret` for any shared environment), `AUTH_KEYCLOAK_ISSUER`, `AUTH_KEYCLOAK_ID`, `INPROOM_API_URL`. After changing the API, run `make contracts` and `cd apps/web && npm run gen:api` so the typed client matches (CI checks both).

### Tenant isolation model

The API connects as the database owner but runs every request transaction as the restricted role `inproom_app` with `app.tenant_id` set from the verified token (`app.db.tenant_session`). Row-level security is **forced** on every tenant table, so a query without a tenant returns no rows. `audit_events` is insert-only for the app role.

### API authentication settings

| Variable | Default |
|---|---|
| `INPROOM_OIDC_ISSUER` | `http://localhost:18080/realms/inproom` |
| `INPROOM_OIDC_AUDIENCE` | `inproom-api` |
| `INPROOM_OIDC_JWKS_URL` | `<issuer>/protocol/openid-connect/certs` |

The real-Keycloak test (`tests/test_keycloak_integration.py`) runs when `INPROOM_TEST_KEYCLOAK_URL` is set (local `.env`); CI skips it and covers token verification with locally signed test tokens.

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
