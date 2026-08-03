# ADR-0004: Browser OAuth ships dark at launch, behind `ADE_OAUTH=1`

Date: 2026-07-23 · Status: superseded by ADR-0008 (the platform
deployed OAuth authz; the gate is deleted and browser login is public)

## Context

The CLI's browser login (authorization-code + PKCE against Logto,
ADR-0002's method menu) works end to end as a *login*: the provider
issues access + refresh tokens. But the platform edge has not deployed
OAuth **authz** to production or eu — a live smoke on 2026-07-23 showed
the eu API rejecting a freshly minted OIDC access token with 401
"Invalid API Key" (and the CLI's automatic refresh-and-retry rejected
too), while the same request with an API key succeeded. Shipping a login
method whose credential no public environment accepts would make a new
user's very first command fail.

## Decision

API keys are the only public login method at launch. The browser flow
stays in the tree — code, provider defaults, tests — but every path to
it (the ADR-0002 method menu, the non-TTY browser fallback) is gated on
`ADE_OAUTH=1` (`auth.py::_oauth_enabled`, checked first inside
`_oauth_can_work`):

- Flagless `auth login` on a terminal goes straight to the hidden
  API-key prompt — no menu, no "browser unavailable" notice (with the
  gate closed, the key prompt is simply *the* method).
- Flagless `auth login` without a terminal fails with a remediation
  naming `--api-key` / `ADE_API_KEY` instead of starting a browser flow.
- With `ADE_OAUTH=1` (internal use, against environments that accept
  OIDC tokens) the ADR-0002 menu and the non-TTY browser path behave
  exactly as before.

Public docs (README) describe API-key login only; the gate is
documented here and in `CONTEXT.md`, not in user-facing docs.

## Consequences

- Flip-back is deleting the gate (and restoring the README auth
  section) once the platform deploys OAuth authz to production/eu —
  the flow, its tests, and the baked client ids are already current.
- ADR-0002's menu decision stands; it simply applies only while the
  gate is open.
- Stored OAuth sessions (from gate-on logins) still authenticate
  requests if present — the gate controls *acquiring* a browser
  credential, not using one.
