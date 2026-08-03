# ADR-0008: Browser OAuth is a public login method (ADR-0004's gate deleted)

Date: 2026-08-03 · Status: accepted · Supersedes: ADR-0004

## Context

ADR-0004 shipped browser login dark behind `ADE_OAUTH=1` because the
platform edge had not deployed OAuth authz to production/eu — a freshly
minted OIDC access token was rejected with 401 while an API key
succeeded. The flip-back condition was explicit: delete the gate once
the platform accepts OIDC tokens. That condition has been met and
verified against the public environments.

## Decision

The gate is deleted (`ADE_OAUTH`, `auth.py::_oauth_enabled`), exactly
the flip-back ADR-0004 planned. Flagless `ade auth login` / `ade login`
now behaves the way a gate-on invocation always did:

- On a terminal: the ADR-0002 method menu (API key first and default,
  browser OAuth second), with the browser option hidden when it cannot
  work for the target (no client_id; raw `ADE_ENDPOINT` without a
  `resource` override).
- Off a terminal: a piped key still wins (`echo $KEY | ade auth
  login`); otherwise the login falls to the browser flow, whose own
  checks diagnose misconfiguration and headless environments.

The `no_credential` remediation branch is retired with the gate — it
existed only for the gate-closed non-TTY dead end. Its job moves to the
browser flow's `no_browser` failure, whose JSON payload now names every
headless spelling (the pipe, `--api-key`, `ADE_API_KEY`), keeping F2
("headless setup never dead-ends") intact.

The README auth section documents browser login again, per ADR-0004's
flip-back note.

## Consequences

- ADR-0002's menu decision now applies unconditionally; ADR-0004 is
  superseded and stays as the record of why launch was API-key-only.
- `ADE_OAUTH=1` in an environment or script is inert — the behavior it
  used to opt into is simply the behavior.
- Stored OAuth sessions keep working as before; the gate never
  controlled *using* a browser credential, only acquiring one.
