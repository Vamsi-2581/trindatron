# Contributing to TRINDATRON

Thanks for helping! TRINDATRON is maintained by one person, so small, focused
pull requests with tests get reviewed fastest. Contributions in English,
Portuguese or Spanish are all welcome.

## Ground rules

- Be kind — everyone here follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- Never commit secrets, `.env` files, databases or logs. Use fake values in tests
  (see `packages/core/tests/unit/test_observability_redaction.py` for the style).
- Security problems go through [SECURITY.md](SECURITY.md), not public issues.
- By contributing you agree that your contribution is licensed under the
  [AGPL-3.0](LICENSE), the license of this project.

## Setting up

Follow the [Quickstart](README.md#quickstart-5-minutes). In short:

```bash
cd packages/core && uv sync --dev && uv run alembic upgrade head
cd ../.. && npm install
```

> The Core installs `packages/model-router` as a non-editable path dependency.
> After editing the router, refresh the Core venv with
> `uv sync --dev --reinstall-package trinity-model-router` (run inside `packages/core`).

## Before you open a pull request

Run the checks for what you touched:

| Area | Command |
|---|---|
| Backend | `cd packages/core && uv run pytest -q` |
| Model router | `cd packages/model-router && uv run pytest -q` |
| Agent | `cd packages/agent && uv run pytest -q` |
| Web | `cd packages/web && npm run typecheck && npm test` |

CI runs the backend suite (SQLite + PostgreSQL-only tests), the router suite and
the web typecheck/unit tests on every pull request.

## How we write code here

- **Tests come with the change.** A bug fix starts with a test that fails
  without it. Many tests explain *why* a rule exists — read them before changing
  behaviour.
- **Match the surrounding code**: naming, comment density, idioms. Comments
  explain *why*, not *what*.
- **Cost and language are product rules.** Changes to model selection must not
  lower a quality floor or bypass plan limits (see
  [docs/ADAPTIVE_INTELLIGENCE_ROUTER.md](docs/ADAPTIVE_INTELLIGENCE_ROUTER.md)).
  User-facing text must exist in `pt`, `en` and `es`.
- **Database changes** need an Alembic migration in `packages/core/alembic/versions`.
- Many comments and docs are still in Portuguese. Translating them to English
  is a great first contribution — keep the meaning exact.

## Commits and pull requests

- One topic per pull request; describe *what* changed and *why*.
- Commit messages: imperative and specific, e.g.
  `fix(router): keep the plan quality floor when a provider is down`.
- Link the issue you are solving (`Fixes #123`).
- Screenshots for UI changes are appreciated.

## Where to start

Look for issues labelled `good first issue` or `help wanted`. If you want to work
on something bigger, open an issue first so we can agree on the approach.
