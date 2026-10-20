# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub: open the repository's **Security** tab and click
**Report a vulnerability** (GitHub private vulnerability reporting). Include:

- what is affected (file, endpoint or package) and the version / commit;
- steps to reproduce or a proof of concept;
- the impact you expect (data exposure, auth bypass, sandbox escape, cost abuse…).

You can expect an acknowledgement within **7 days** and a status update within
**30 days**. Once a fix is released we will credit you in the advisory unless you
prefer to stay anonymous.

## Supported versions

TRINDATRON has no stable release yet. Security fixes land on the default branch
(`main`); please test against it before reporting.

## Scope

In scope: everything in this repository — the Core API, model router, sandbox,
agent, web and desktop apps, and the default configuration.

Particularly interesting areas:

- authentication, sessions and account recovery (`packages/core/src/api/routes/auth*.py`)
- object-level authorization between users and teams
- sandbox isolation (`packages/sandbox`, `AGENT_SANDBOX_*` settings)
- SSRF and network policy in the browser agent and URL fetchers
- plan limits and cost caps in the model router (abuse that makes the operator pay)
- secrets leaking into logs, traces or error messages

Out of scope: vulnerabilities in third-party providers themselves, issues that
require a compromised developer machine, and missing hardening on a local
`ENVIRONMENT=development` setup.

## For operators

- Never run with `ENVIRONMENT=development` or `DEBUG=true` on the internet.
- Use PostgreSQL, a random `JWT_SECRET_KEY` of 32+ characters and explicit
  `ALLOWED_ORIGINS`; the backend refuses to start in production otherwise.
- Keep provider keys in environment variables or a secret manager, never in the
  repository.
