# TRINDATRON

Agentic AI platform with streaming SSE chat, code workspaces and autonomous tasks.

**Stack:** FastAPI · SQLAlchemy · Alembic · SQLite/PostgreSQL · Next.js 14 · Electron · TypeScript

---

## Prerequisites

| Tool | Minimum version | Check |
|---|---|---|
| Python | 3.12+ | `python --version` |
| uv | any | `uv --version` |
| Node.js | 20+ | `node --version` |

> **Windows:** The commands below work in PowerShell. Bash through Git Bash is also supported.

---

## Quick start — Backend (TRINITY CORE)

```powershell
cd packages\core

# 1. Install Python dependencies
uv sync

# 2. Copy and fill in the environment variables
copy .env.example .env
# Edit .env and set ANTHROPIC_API_KEY (required for chat to work)

# 3. Apply migrations — REQUIRED on first run and after updates
uv run alembic upgrade head

# 4. Start the server
uv run uvicorn src.main:app --reload --port 8000
# API available at: http://localhost:8000
# Health check:     http://localhost:8000/health
```

> **Note:** If the backend starts without `alembic upgrade head`, every route that accesses the database returns 500. Always run the migrations before the first startup in a clean environment.

---

## Quick start — Web frontend (TRINITY WEB)

```powershell
cd packages\web

# 1. Install Node dependencies
npm install

# 2. The .env.local file already points to http://localhost:8000 (local backend)
# To use another backend: edit packages/web/.env.local
#   NEXT_PUBLIC_API_URL=http://localhost:8000

# 3. Start in development mode
npm run dev
# Available at: http://localhost:3000
```

---

## Required environment variables

### Backend — `packages/core/.env`

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Chat and agent features fail without it |
| `JWT_SECRET_KEY` | In production | At least 32 characters; cannot be the placeholder |
| `DATABASE_URL` | In production | PostgreSQL is required in production (`sqlite://` is accepted in development) |
| `ENVIRONMENT` | In production | `production` enables startup security checks |
| `ALLOWED_ORIGINS` | In production | JSON array of domains; no localhost |

### Health endpoints

| Endpoint | Access | Contents |
|---|---|---|
| `/health` | public | Liveness; used by Railway's health check |
| `/health/db` | public | Readiness; checks the database connection |
| `/health/metrics` | **admin** | Aggregates of traffic, plans, provider/model, cost/TCU and capability demand |

`/health/metrics` requires a user with `is_admin=True` (the same JWT and the same gate in `src/api/deps.py`). The values are aggregated — never message content, session/user IDs or credentials — but they describe the product's economics, so they are not public.

### Frontend — `packages/web/.env.local`

| Variable | Default | Description |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend URL without a trailing slash |

---

## Migrations

```powershell
cd packages\core

# Apply all pending migrations
uv run alembic upgrade head

# Show the current database revision
uv run alembic current

# Show migration history
uv run alembic history
```

The database uses **SQLite in development** and requires **PostgreSQL in production** (SQLite does not support concurrent writes). The backend refuses to start in `ENVIRONMENT=production` with SQLite.

---

## Development commands

### Backend

```powershell
cd packages\core

# Server with auto-reload
uv run uvicorn src.main:app --reload --port 8000

# Tests
uv run pytest tests/ -v

# Helper scripts (PowerShell)
.\..\..\scripts\dev-core.ps1
.\..\..\scripts\test-core.ps1
```

### Model Router (Adaptive Intelligence Router)

```powershell
cd packages\model-router

uv run pytest -q     # full suite
```

Architecture, failover invariants, Quality Recovery and the safety rule for legitimate refusals: `docs/ADAPTIVE_INTELLIGENCE_ROUTER.md`.

> **Caution — path dependency.** `packages/core` installs `packages/model-router` as a copy (not an editable install). After editing the router, `uv sync` alone does **not** update the Core virtual environment because the version has not changed. Run:
>
> ```powershell
> cd packages\core
> uv sync --dev --reinstall-package trinity-model-router
> ```
>
> CI and production Docker builds start from a clean checkout and are not affected.

### Web frontend

```powershell
cd packages\web

npm run dev          # development (http://localhost:3000)
npm run typecheck    # TypeScript check (tsc --noEmit)
npm run test         # unit tests (vitest)
npm run build        # production build (next build)
npm run start        # serve the production build
```

### Desktop (Electron)

```powershell
cd packages\desktop

npm install
npm run dev          # opens an Electron window in development mode
npm run build        # production build
```

---

## Production — minimum checklist

The backend **refuses to start** if any item below is wrong when `ENVIRONMENT=production`:

- [ ] `DATABASE_URL` points to PostgreSQL
- [ ] `JWT_SECRET_KEY` has 32+ characters and is not the placeholder
- [ ] `ANTHROPIC_API_KEY` is set
- [ ] `DEBUG=false`
- [ ] `ALLOWED_ORIGINS` contains no localhost entries

Generate a secure JWT secret:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Windows note — litellm cleanup

When tests end, this may appear in the console:

```
ValueError: I/O operation on closed file.
```

This is a cosmetic error from litellm's asynchronous cleanup on Windows. The test exit code is always 0. It does not affect functionality or CI.
