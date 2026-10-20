## What and why

<!-- What does this change, and why is it needed? Link the issue: Fixes #123 -->

## How it was tested

- [ ] `packages/core`: `uv run pytest -q`
- [ ] `packages/model-router`: `uv run pytest -q`
- [ ] `packages/web`: `npm run typecheck && npm test`
- [ ] Not applicable (docs only)

## Checklist

- [ ] Tests cover the change (a bug fix includes a test that fails without it)
- [ ] User-facing text exists in pt, en and es
- [ ] No secrets, `.env` files, databases or logs are included
- [ ] Database changes include an Alembic migration
