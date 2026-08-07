# ADR-0009: Organization selection rides `x-org-id`, not Logto organization tokens

Date: 2026-08-07 · Status: accepted

## Context

A browser login's access token is organization-blind: the CLI requests
no organization at the token grant, so the platform attributes every
request to the account's server-side default organization (its
`migratedOrgId`). Users who belong to several Logto organizations have
no way to choose — a bug for any multi-org account.

Two transports could carry a selection:

1. **A request header.** The platform's authz already honors `x-org-id`
   for user-scoped credentials (OIDC access tokens and PATs), verifying
   membership on every request. No server change needed.
2. **Logto organization tokens.** Mint the access token with the
   `organization_id` grant parameter, putting the org in a *signed*
   claim, with membership enforced by Logto at mint time. Requires the
   platform's authz to read the claim — a change it has not shipped.

Both need the same CLI work (the organizations scope, discovery via
userinfo, a stored selection, a switch command); they differ only in
transport. The header is re-verified per request (a removed member fails
immediately); the claim is cheaper server-side and auditable in the
token, but stale for up to a token lifetime after a membership change.

## Decision

The selection rides the `x-org-id` header (option 1), stored on the
OAuth session as `organization: {id, name}` in `credentials.json` and
sent by the gateway on every API request. API keys never send it — they
are already organization-bound.

Discovery is Logto's userinfo: login requests
`urn:logto:scope:organizations`, then spends one refresh **without** the
RFC 8707 resource indicator (the API-audience JWT is unwelcome at
userinfo; only the opaque token gets in) and reads the `organizations` /
`organization_data` claims. One membership selects itself; several
prompt on a terminal or defer to `--org`; discovery failing never fails
a login — the platform default applies until `auth org switch` sets one.
Sessions predating the scope change discover nothing (the claim is
absent, not empty) and are told to re-login.

## Consequences

- Selection and switching work with zero platform changes; membership
  stays server-verified per request, so a forged or stale header cannot
  cross an org boundary.
- Flip condition, recorded now: when the platform's authz reads the
  `organization_id` claim from verified OIDC tokens, the CLI should move
  the transport to the token grant (`organization_id` parameter) and
  drop the header. Nothing user-visible changes — the stored selection,
  discovery, and commands all stay.
- The platform's shadow authz service ignores `x-org-id` today
  (comparison noise, no runtime effect); flagged to the platform team as
  a cutover prerequisite.
- Every browser login costs two extra Logto round-trips (the
  resource-less refresh, userinfo). Refresh-token rotation makes the
  discovery refresh a real spend: the rotated token is persisted under
  the same cross-process lock as every other refresh.
