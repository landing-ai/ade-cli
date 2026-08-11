"""``auth`` group: login, status, logout.

Nothing about the target is ever stored (ADR-0003): every auth command
resolves it fresh — ``--env`` flag → ``ADE_ENV`` → production — the same
rule ``parse``/``extract`` follow, so the environment a login
authenticates is exactly the one a later verb in the same shell will
use. Credentials are stored per environment and coexist.

``login`` is a guarantee command — "ensure logged in on the target".
``--api-key KEY`` (``--api-key -`` for a hidden prompt) authenticates it
with a key directly. Without a credential flag it reports an existing
stored credential (nothing to do) or acquires one — an API key prompt on
a terminal, a key piped on stdin off one (``echo $KEY | ade auth
login``), and otherwise the ``ADE_API_KEY`` remediation. Headless setup
never dead-ends on a missing TTY.
An API key is verified live before it is stored (#117): one authenticated
no-op POST to the telemetry route. A 401 there is the platform saying the
key is invalid — the login fails with one canonical message and stores
nothing, instead of deferring the discovery to the first ``parse``. Any
other failure (5xx, unreachable network) says nothing about the key, so
the login reports the platform problem and stores nothing.
Browser OAuth is a public login method (ADR-0008 deleted ADR-0004's
launch gate): a terminal gets the ADR-0002 method menu, and a
non-interactive run with no piped key falls to the browser flow, whose
own checks diagnose a headless environment.

``logout`` de-auths one environment (the resolved target by default;
``--all`` clears every environment). ``status`` reports the resolved
target plus every other environment holding a credential.

Browser logins carry an organization selection (ADR-0009): memberships
are discovered from Logto's userinfo after the token exchange, the
selection is stored on the OAuth session, and it rides ``x-org-id`` on
API requests (membership-verified by the platform per request). One
membership selects itself; several prompt on a terminal (``--org`` picks
without one); discovery failing never fails a login — the platform
default organization applies until ``auth org switch`` sets one.
"""

from __future__ import annotations

import os
import sys

import httpx
import typer

from . import credentials, filelock, gateway, oauth, term
from .config import (
    DEFAULT_ENVIRONMENT,
    ENVIRONMENTS,
    ResolvedConfig,
    ade_home,
    config_path,
    load_config,
    resolve_target,
    validate_environment,
)
from .output import EXIT_FAILED, EXIT_USAGE, JSON_FLAG, emit, exit_with
from .ports import Ports

auth_app = typer.Typer(name="auth", no_args_is_help=True, help="Authenticate ade.")

_ENV_NAMES = ", ".join(ENVIRONMENTS)

_ENV_HELP = (
    f"Environment to target: {_ENV_NAMES} (default: $ADE_ENV, then "
    "production). Credentials are stored per environment."
)



@auth_app.command()
def login(
    ctx: typer.Context,
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Log in with this API key directly "
        "('-' prompts with hidden input).",
    ),
    org: str | None = typer.Option(
        None,
        "--org",
        help="Act in this Logto organization (id or name). Browser "
        "(OAuth) logins only — API keys are already organization-bound.",
    ),
    environment: str | None = typer.Option(None, "--env", help=_ENV_HELP),
    as_json: bool = JSON_FLAG,
) -> None:
    """Ensure the target environment is logged in; `--api-key`
    authenticates with a key directly ('-' prompts with hidden input).
    Targets `--env`, else $ADE_ENV, else production — nothing is stored
    about the choice."""
    home = ade_home()
    resolved = resolve_target(home, environment, as_json=as_json)
    if api_key is not None and org is not None:
        exit_with(
            {
                "error": "org_with_api_key",
                "message": "--org applies to browser (OAuth) logins; API "
                "keys are already organization-bound.",
            },
            "--org applies to browser (OAuth) logins; API keys are already "
            "organization-bound.",
            as_json=as_json,
            code=EXIT_USAGE,
        )
    if api_key is None:
        _login_without_key(ctx.obj, home, resolved, org=org, as_json=as_json)
        return

    if api_key == "-":
        api_key = _prompt_api_key(ctx.obj)
    _finish_api_key_login(ctx.obj, home, api_key, resolved, as_json=as_json)


def _prompt_api_key(ports: Ports) -> str:
    """The hidden-input key prompt. Off a terminal the "prompt" is simply
    the piped line: getpass would either reach past the pipe to /dev/tty
    or fall back with a GetPassWarning on stderr, and neither is what
    ``echo $KEY | ade auth login --api-key -`` asked for."""
    if not ports.stdin_is_tty():
        return sys.stdin.readline()
    # Prompt on stderr so --json stdout stays one stable object.
    return typer.prompt("API key", hide_input=True, err=True)


# How long a headless login waits for a key that was piped in. Zero would
# lose the race with a writer that hasn't been scheduled yet; the wait is
# paid only on the path that would otherwise exit with an error anyway.
_PIPED_KEY_TIMEOUT = 0.25

def _piped_api_key(ports: Ports) -> str | None:
    """A key piped on stdin (``echo $KEY | ade auth login``), or None.

    Never blocks on an idle pipe: stdin is read only once the platform
    probe (select on POSIX, PeekNamedPipe on Windows — #168) says a line
    is already there, so a harness that leaves stdin open but silent gets
    the remediation immediately instead of a hang.
    """
    if ports.stdin_is_tty():
        return None
    try:
        fileno = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        # Not a real OS stream (an in-process runner's buffer): readable
        # by construction, and reading it cannot block on a writer.
        fileno = None
    if fileno is not None and not filelock.stdin_ready(
        fileno, timeout=_PIPED_KEY_TIMEOUT
    ):
        return None
    return sys.stdin.readline().strip() or None


def _finish_api_key_login(
    ports: Ports, home, api_key: str, resolved: ResolvedConfig, *, as_json: bool
) -> None:
    """Verify and store a key for the resolved target. The target is
    never prompted for: the one resolution rule already named it. Nothing
    is stored until the platform has accepted the key (#117)."""
    if not api_key.strip():
        emit(
            {"error": "empty_api_key", "message": "API key must not be empty."},
            "API key must not be empty.",
            as_json=as_json,
        )
        raise typer.Exit(code=EXIT_USAGE)
    api_key = api_key.strip()
    _verify_api_key(ports, api_key, resolved, as_json=as_json)
    credentials.store_api_key(home, api_key, resolved.environment)
    masked = credentials.mask(api_key)
    emit(
        {
            "method": "api_key",
            "credential": masked,
            "verified": True,
            "stored": True,
            "environment": resolved.environment,
            "endpoint": resolved.endpoint,
            "endpoint_source": resolved.endpoint_source,
        },
        f"Logged in with API key {masked} (verified, stored in "
        f"{credentials.credentials_path(home)} for the "
        f"{resolved.environment} environment).\n"
        f"Endpoint: {resolved.endpoint} ({resolved.endpoint_source})",
        as_json=as_json,
    )


def _verify_api_key(
    ports: Ports, api_key: str, resolved: ResolvedConfig, *, as_json: bool
) -> None:
    """The live check behind #117, before anything is stored. Exactly one
    answer is authoritative about the key — the target's 401 — and it gets
    the one canonical invalid-key message (the platform's own 401 bodies
    vary by which check rejected the key; that inconsistency stays out of
    the CLI's mouth). Every other failure is reported as the platform
    problem it is, and the key is neither blamed nor stored."""
    try:
        gateway.verify_credential(
            endpoint=resolved.endpoint,
            secret=api_key,
            transport=ports.transport,
            command="auth",
        )
    except gateway.GatewayError as error:
        if error.status_code == 401:
            exit_with(
                {
                    "error": "invalid_api_key",
                    "status_code": 401,
                    "environment": resolved.environment,
                    "endpoint": resolved.endpoint,
                    "message": error.detail,
                },
                f"This API key was rejected by {resolved.endpoint_label} "
                "(HTTP 401): it is invalid or revoked. Check that the key "
                "is complete and entered correctly, then try again. "
                "Nothing was stored.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        exit_with(
            {
                "error": "verification_failed",
                "status_code": error.status_code,
                "code": error.code,
                "environment": resolved.environment,
                "endpoint": resolved.endpoint,
                "message": error.detail,
            },
            f"Could not verify the API key against {resolved.endpoint_label}: "
            f"HTTP {error.status_code}: {error.detail}. Something went wrong "
            "on the platform side — try again later. Nothing was stored.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    except httpx.HTTPError as error:
        exit_with(
            {
                "error": "verification_unreachable",
                "environment": resolved.environment,
                "endpoint": resolved.endpoint,
                "message": str(error),
            },
            f"Could not reach {resolved.endpoint_label} to verify the API "
            f"key ({type(error).__name__}: {error}). Check your network and "
            "try again. Nothing was stored.",
            as_json=as_json,
            code=EXIT_FAILED,
        )


def _login_without_key(
    ports: Ports, home, resolved: ResolvedConfig, *, org: str | None, as_json: bool
) -> None:
    """No credential flag: *ensure* logged in on the target. A stored
    credential means there is nothing to do (credentials are per
    environment; no selection exists to change) — unless --org names an
    organization, which the guarantee then ensures too. Otherwise acquire
    one — a terminal prompts (the ADR-0002 method menu); a non-interactive
    run takes a key piped on stdin, else the browser flow. --org implies
    the browser method, so it skips both the pipe and the menu."""
    existing = credentials.stored_credential(home, resolved.environment)
    if existing is not None:
        if org is not None:
            _ensure_org_on_existing(
                ports, home, resolved, existing, org, as_json=as_json
            )
            return
        _emit_already(home, resolved, existing, as_json=as_json)
        return
    if org is not None:
        _browser_login(ports, home, resolved, org=org, as_json=as_json)
        return
    # A key piped in is a prompt answered ahead of time — the headless
    # spelling of the same gesture (F2) — and it wins *before* the menu:
    # a normal shell pipe (`echo $KEY | ade auth login`) leaves stderr on
    # the terminal, and the menu would eat the key as its raw selection.
    # Costless when stdin is a tty or idle (_piped_api_key never blocks).
    piped = _piped_api_key(ports)
    if piped is not None:
        _finish_api_key_login(ports, home, piped, resolved, as_json=as_json)
        return
    if ports.stderr_is_tty():
        _acquire_interactively(ports, home, resolved, as_json=as_json)
        return
    # Straight to the browser flow, whose own checks diagnose
    # misconfiguration authoritatively (client_id, resource, no browser).
    _browser_login(ports, home, resolved, as_json=as_json)


def _acquire_interactively(
    ports: Ports, home, resolved: ResolvedConfig, *, as_json: bool
) -> None:
    """The login menu, on stderr like every prompt. API key is option 1
    and the default — the method every target supports from day one — and
    browser OAuth is offered only when it can work for the target, so an
    API-key-only rollout (an environment with no client_id yet) collapses
    to the key prompt alone. The chosen flow re-checks configuration
    authoritatively; this check only keeps dead options out of the
    menu."""
    if _oauth_can_work(home, resolved):
        if _choose_method(ports) == "oauth":
            _browser_login(ports, home, resolved, as_json=as_json)
            return
    else:
        typer.echo(
            "Browser sign-in isn't available for this target; use an API key.",
            err=True,
        )
    _finish_api_key_login(
        ports, home, _prompt_api_key(ports), resolved, as_json=as_json
    )


_METHOD_LABELS = (
    "1) Paste an API key (hidden input)",
    "2) Sign in with your browser (OAuth)",
)


def _choose_method(ports: Ports) -> str:
    """API key is first and the default either way, so bare Enter continues
    with it. A real terminal gets the arrow-key pointer (digits jump, Enter
    confirms); TERM=dumb and terminals whose raw mode fails get the same
    list numbered with a typed prompt. (Piped stdin never reaches the
    menu — a piped line is the key itself, consumed before this.)"""
    if ports.stdin_is_tty() and os.environ.get("TERM") != "dumb":
        typer.echo("How would you like to log in? (↑/↓ and Enter)", err=True)
        try:
            index = term.select(list(_METHOD_LABELS), getchar=ports.getchar)
            return "api_key" if index == 0 else "oauth"
        except term.Unsupported:
            pass  # the widget erased itself; the typed fallback re-lists
    else:
        typer.echo("How would you like to log in?", err=True)
    for label in _METHOD_LABELS:
        typer.echo(f"  {label}", err=True)
    while True:
        choice = typer.prompt("Method", default="1", err=True).strip()
        if choice in ("1", "2"):
            return "api_key" if choice == "1" else "oauth"
        typer.echo("Choose 1 or 2.", err=True)


def _oauth_can_work(home, resolved: ResolvedConfig) -> bool:
    """Whether the browser flow could succeed for the target: a client_id
    exists, and — under an ``ADE_ENDPOINT`` override — an explicit
    ``resource``, since a raw URL has no environment to infer the token
    audience from. Mirrors _run_browser_flow's checks, which stay
    authoritative."""
    if not oauth.resolve_provider(home, resolved.environment).client_id:
        return False
    if resolved.endpoint_source != "env":
        return True
    override = (load_config(home).get("oauth") or {}).get(resolved.environment) or {}
    return bool(override.get("resource"))


def _emit_already(
    home, resolved: ResolvedConfig, cred: credentials.ActiveCredential, *, as_json: bool
) -> None:
    """The ensure short-circuit: the target already holds a credential."""
    payload = {
        "method": cred.method,
        "credential": cred.masked,
        "already_authenticated": True,
        "stored": True,
        "environment": resolved.environment,
        "endpoint": resolved.endpoint,
        "endpoint_source": resolved.endpoint_source,
    }
    tail = (
        f"Endpoint: {resolved.endpoint} ({resolved.endpoint_source})\n"
        f"Force a fresh login with `ade auth logout --env "
        f"{resolved.environment}` first."
    )
    if cred.method == "oauth" and cred.oauth is not None:
        identity = cred.oauth.get("identity") or {}
        who = identity.get("email") or identity.get("sub") or "unknown identity"
        payload["identity"] = identity
        # The published `auth login` shape promises `organization` on every
        # OAuth result, so the no-op path carries it too — null included,
        # since agents read the shape rather than discovering it.
        organization = cred.oauth.get("organization") or None
        payload["organization"] = organization
        org_note = (
            _org_label(organization)
            if organization
            else "platform default (none selected)"
        )
        human = (
            f"Already authenticated for the {resolved.environment} environment "
            f"as {who}; nothing to do.\nOrganization: {org_note}\n{tail}"
        )
    else:
        human = (
            f"Already authenticated for the {resolved.environment} environment "
            f"({cred.method} {cred.masked}); nothing to do.\n{tail}"
        )
    emit(payload, human, as_json=as_json)


def _browser_login(
    ports: Ports,
    home,
    resolved: ResolvedConfig,
    *,
    org: str | None = None,
    as_json: bool,
) -> None:
    entry = _run_browser_flow(ports, home, resolved, as_json=as_json)
    credentials.store_oauth(home, resolved.environment, entry)
    # Tokens are durable before the org pick starts: a failed or skipped
    # selection degrades to the platform default, never to a lost login.
    organization, note = _select_org_after_login(
        ports, home, resolved, requested=org, as_json=as_json
    )
    _emit_browser_login(
        home, resolved, entry, organization=organization, note=note, as_json=as_json
    )


def _org_env_suffix(resolved: ResolvedConfig) -> str:
    """`--env X` for a non-default target, mirroring login_hint's rule."""
    if resolved.environment != DEFAULT_ENVIRONMENT:
        return f" --env {resolved.environment}"
    return ""


def _org_switch_hint(resolved: ResolvedConfig) -> str:
    """The `auth org switch` invocation for *this* target."""
    return f"ade auth org switch <org>{_org_env_suffix(resolved)}"


def _org_clear_hint(resolved: ResolvedConfig) -> str:
    """The `auth org clear` invocation for *this* target."""
    return f"ade auth org clear{_org_env_suffix(resolved)}"


def _select_org_after_login(
    ports: Ports,
    home,
    resolved: ResolvedConfig,
    *,
    requested: str | None,
    as_json: bool,
) -> tuple[dict | None, str | None]:
    """The post-login org pick: (selection, note-for-humans). An explicit
    --org that cannot be honored fails the command (the user asked for a
    state the login didn't reach — though the tokens are stored); the
    automatic pick degrades to a note instead, because a login must never
    be lost to a discovery hiccup."""
    try:
        organizations = oauth.fetch_organizations(home, resolved.environment, ports)
    except oauth.OrgDiscoveryError as error:
        if requested is not None:
            exit_with(
                {
                    "error": f"org_{error.reason}",
                    "message": error.message,
                    "stored": True,
                },
                f"Logged in, but selecting an organization failed: "
                f"{error.message}. The tokens are stored; run "
                f"`{_org_switch_hint(resolved)}` once the problem is fixed.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        return None, (
            f"Organization discovery failed ({error.message}); the platform "
            f"default applies. Select one later with "
            f"`{_org_switch_hint(resolved)}`."
        )
    if requested is not None:
        organization = _match_org(
            organizations, requested, resolved=resolved, as_json=as_json
        )
        oauth.set_organization(home, resolved.environment, organization)
        return organization, None
    if not organizations:
        return None, None
    if len(organizations) == 1:
        oauth.set_organization(home, resolved.environment, organizations[0])
        return organizations[0], None
    organization = _prompt_org_choice(ports, organizations)
    if organization is None:
        return None, (
            "Your account belongs to multiple organizations; none was "
            "selected, so the platform default applies. Choose one with "
            f"`{_org_switch_hint(resolved)}`."
        )
    oauth.set_organization(home, resolved.environment, organization)
    return organization, None


def _org_label(organization: dict) -> str:
    name = organization.get("name") or organization["id"]
    if name == organization["id"]:
        return organization["id"]
    return f"{name} ({organization['id']})"


def _prompt_org_choice(ports: Ports, organizations: list[dict]) -> dict | None:
    """The org menu, shaped like the login-method menu: arrow-key pointer
    on a real terminal, a numbered typed prompt as the fallback. Without
    an interactive stdin there is nobody to ask — return None and let the
    caller leave the platform default in place."""
    if not ports.stdin_is_tty():
        return None
    labels = [_org_label(organization) for organization in organizations]
    if os.environ.get("TERM") != "dumb":
        typer.echo(
            "Which organization should this login act in? (↑/↓ and Enter)",
            err=True,
        )
        try:
            index = term.select(labels, getchar=ports.getchar)
            return organizations[index]
        except term.Unsupported:
            pass  # the widget erased itself; the typed fallback re-lists
    else:
        typer.echo("Which organization should this login act in?", err=True)
    for number, label in enumerate(labels, start=1):
        typer.echo(f"  {number}) {label}", err=True)
    while True:
        choice = typer.prompt("Organization", default="1", err=True).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(organizations):
            return organizations[int(choice) - 1]
        typer.echo(f"Choose 1-{len(organizations)}.", err=True)


def _match_org(
    organizations: list[dict],
    requested: str,
    *,
    resolved: ResolvedConfig,
    as_json: bool,
) -> dict:
    """Resolve an id-or-name to one membership: exact id first, then a
    unique case-insensitive name. Anything else exits listing what the
    account can actually act in (the server would reject it anyway —
    x-org-id is membership-verified per request)."""
    for organization in organizations:
        if organization["id"] == requested:
            return organization
    by_name = [
        organization
        for organization in organizations
        if (organization.get("name") or "").casefold() == requested.casefold()
    ]
    if len(by_name) == 1:
        return by_name[0]
    listed = ", ".join(_org_label(o) for o in organizations) or "none"
    reason = "org_ambiguous" if len(by_name) > 1 else "org_not_found"
    detail = (
        f"{requested!r} names more than one organization — pass its id"
        if len(by_name) > 1
        else f"no organization of yours matches {requested!r}"
    )
    exit_with(
        {
            "error": reason,
            "requested": requested,
            "organizations": organizations,
            "environment": resolved.environment,
        },
        f"Cannot select an organization: {detail}. Your organizations: "
        f"{listed}.",
        as_json=as_json,
        code=EXIT_FAILED,
    )


def _ensure_org_on_existing(
    ports: Ports,
    home,
    resolved: ResolvedConfig,
    existing: credentials.ActiveCredential,
    requested: str,
    *,
    as_json: bool,
) -> None:
    """`login --org X` against a target that is already logged in: the
    guarantee extends to the organization, so an OAuth session switches
    without a browser round-trip (the refresh token is org-agnostic)."""
    if existing.method != "oauth" or existing.oauth is None:
        exit_with(
            {
                "error": "org_with_api_key",
                "message": "--org applies to browser (OAuth) logins; the "
                "stored credential is an API key, which is already "
                "organization-bound.",
            },
            "--org applies to browser (OAuth) logins; the stored credential "
            "is an API key, which is already organization-bound. Log out "
            "first to switch methods.",
            as_json=as_json,
            code=EXIT_USAGE,
        )
    try:
        organizations = oauth.fetch_organizations(home, resolved.environment, ports)
    except oauth.OrgDiscoveryError as error:
        exit_with(
            {"error": f"org_{error.reason}", "message": error.message},
            f"Selecting an organization failed: {error.message}.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    organization = _match_org(
        organizations, requested, resolved=resolved, as_json=as_json
    )
    oauth.set_organization(home, resolved.environment, organization)
    identity = existing.oauth.get("identity") or {}
    who = identity.get("email") or identity.get("sub") or "unknown identity"
    emit(
        {
            "method": "oauth",
            "already_authenticated": True,
            "organization": organization,
            "stored": True,
            "environment": resolved.environment,
            "endpoint": resolved.endpoint,
            "endpoint_source": resolved.endpoint_source,
        },
        f"Already authenticated for the {resolved.environment} environment "
        f"as {who}; organization set to {_org_label(organization)}.",
        as_json=as_json,
    )


def _run_browser_flow(
    ports: Ports, home, resolved: ResolvedConfig, *, as_json: bool
) -> dict:
    provider = oauth.resolve_provider(home, resolved.environment)
    if not provider.client_id:
        exit_with(
            {
                "error": "oauth_not_configured",
                "environment": resolved.environment,
                "message": "No OAuth client_id is configured for this environment.",
            },
            f"OAuth login is not configured for the {resolved.environment} "
            "environment yet (no client_id). Add "
            f'{{"oauth": {{"{resolved.environment}": {{"client_id": "..."}}}}}} '
            f"to {config_path(home)}, or use `ade auth login --api-key <key>`.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    override = (load_config(home).get("oauth") or {}).get(resolved.environment) or {}
    if resolved.endpoint_source == "env" and not override.get("resource"):
        # An ADE_ENDPOINT raw URL has no environment to infer the token
        # audience from — the default resource would mint a token for the
        # wrong API.
        exit_with(
            {
                "error": "oauth_unknown_audience",
                "endpoint": resolved.endpoint,
                "message": "Browser login cannot infer the token audience "
                "for a raw endpoint.",
            },
            f"Browser login cannot infer the token audience for the raw "
            f"endpoint {resolved.endpoint} (ADE_ENDPOINT). Add "
            f'{{"oauth": {{"{resolved.environment}": {{"resource": "..."}}}}}} '
            f"to {config_path(home)}, or use `ade auth login --api-key <key>`.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    try:
        return oauth.login(provider, ports)
    except oauth.BrowserUnavailable:
        # One message for both modes: the JSON payload is what agents
        # read, so the remediation must live in it, not only in the
        # human text.
        human = (
            "Could not open a browser (headless environment?). Pipe the "
            "key in (`echo $KEY | ade auth login`), pass "
            "`ade auth login --api-key <key>`, or set ADE_API_KEY."
        )
        exit_with(
            {"error": "no_browser", "message": human},
            human,
            as_json=as_json,
            code=EXIT_FAILED,
        )
    except oauth.LoginError as error:
        exit_with(
            {"error": f"oauth_{error.reason}", "message": error.message},
            f"Browser login failed: {error.message}.",
            as_json=as_json,
            code=EXIT_FAILED,
        )


def _emit_browser_login(
    home,
    resolved: ResolvedConfig,
    entry: dict,
    *,
    organization: dict | None = None,
    note: str | None = None,
    as_json: bool,
) -> None:
    identity = entry.get("identity") or {}
    who = identity.get("email") or identity.get("sub") or "unknown identity"
    human = (
        f"Logged in as {who} via browser (tokens stored in "
        f"{credentials.credentials_path(home)} for the "
        f"{resolved.environment} environment).\n"
        f"Endpoint: {resolved.endpoint} ({resolved.endpoint_source})"
    )
    if organization is not None:
        human = f"{human}\nOrganization: {_org_label(organization)}"
    if note is not None:
        human = f"{human}\n{note}"
    payload = {
        "method": "oauth",
        "identity": identity,
        "credential": credentials.mask(entry["access_token"]),
        "organization": organization,
        "stored": True,
        "environment": resolved.environment,
        "endpoint": resolved.endpoint,
        "endpoint_source": resolved.endpoint_source,
    }
    if note is not None:
        # Agents read the JSON, so the remediation must live in it too.
        payload["organization_note"] = note
    emit(payload, human, as_json=as_json)


def _expiry_note(oauth_entry: dict, ports: Ports) -> tuple[float | None, str]:
    expires_at = oauth_entry.get("expires_at")
    if expires_at is None:
        return None, "expiry unknown"
    remaining = float(expires_at) - ports.clock.now()
    if remaining <= 0:
        return remaining, "expired (refreshes on next use)"
    if remaining < 3600:
        return remaining, f"expires in {int(remaining // 60)}m"
    return remaining, f"expires in {remaining / 3600:.1f}h"


@auth_app.command()
def status(
    ctx: typer.Context,
    environment: str | None = typer.Option(None, "--env", help=_ENV_HELP),
    as_json: bool = JSON_FLAG,
) -> None:
    """Show the resolved target's auth method, identity, and expiry, plus
    every other environment holding a credential."""
    home = ade_home()
    ports: Ports = ctx.obj
    resolved = resolve_target(home, environment, as_json=as_json)
    active = credentials.resolve(home, resolved.environment)
    if active is None:
        others = _other_environments(home, resolved.environment)
        human = (
            f"Not authenticated for {resolved.endpoint_label}. "
            f"Run `{resolved.login_hint}` or set ADE_API_KEY."
        )
        if others:
            listed = ", ".join(_describe_env(item) for item in others)
            human = f"{human}\nAlso authenticated: {listed} (target with --env)."
        emit(
            {
                "authenticated": False,
                "environment": resolved.environment,
                "endpoint": resolved.endpoint,
                "endpoint_source": resolved.endpoint_source,
                "other_environments": others,
            },
            human,
            as_json=as_json,
        )
        raise typer.Exit(code=EXIT_FAILED)
    source_note = {
        "env": "from ADE_API_KEY (overrides stored credentials)",
        "stored": f"stored in {credentials.credentials_path(home)}",
    }[active.source]
    payload = {
        "authenticated": True,
        "method": active.method,
        "credential": active.masked,
        "source": active.source,
        "environment": resolved.environment,
        "endpoint": resolved.endpoint,
        "endpoint_source": resolved.endpoint_source,
    }
    tail = (
        f"Environment: {resolved.environment}\n"
        f"Endpoint: {resolved.endpoint} ({resolved.endpoint_source})"
    )
    if active.method == "oauth" and active.oauth is not None:
        identity = active.oauth.get("identity") or {}
        remaining, note = _expiry_note(active.oauth, ports)
        refresh = "available" if active.oauth.get("refresh_token") else "unavailable"
        organization = active.oauth.get("organization")
        payload.update(
            {
                "identity": identity,
                "expires_at": active.oauth.get("expires_at"),
                "expires_in_seconds": max(0, int(remaining)) if remaining is not None else None,
                "refresh_token": refresh == "available",
                "organization": organization,
            }
        )
        org_note = (
            _org_label(organization)
            if organization
            else "platform default (none selected)"
        )
        who = identity.get("email") or identity.get("sub") or "unknown identity"
        human = (
            f"Authenticated via OAuth as {who} ({source_note}).\n"
            f"Access token {active.masked} {note}; refresh token {refresh}.\n"
            f"Organization: {org_note}\n"
            f"{tail}"
        )
    else:
        human = (
            f"Authenticated with {active.method} {active.masked} ({source_note}).\n"
            f"{tail}"
        )
    others = _other_environments(home, resolved.environment)
    payload["other_environments"] = others
    if others:
        listed = ", ".join(_describe_env(item) for item in others)
        human = f"{human}\nAlso authenticated: {listed} (target with --env)."
    emit(payload, human, as_json=as_json)


def _other_environments(home, target_env: str) -> list[dict]:
    """Environments (other than the resolved target) with a stored
    credential — what ``--env X`` can use without re-authenticating."""
    result: list[dict] = []
    for env, cred in sorted(credentials.stored_environments(home).items()):
        if env == target_env:
            continue
        item = {"environment": env, "method": cred.method}
        if cred.method == "oauth" and cred.oauth is not None:
            identity = cred.oauth.get("identity") or {}
            who = identity.get("email") or identity.get("sub")
            if who:
                item["identity"] = who
        result.append(item)
    return result


def _describe_env(item: dict) -> str:
    who = item.get("identity")
    detail = f"oauth: {who}" if who else item["method"]
    return f"{item['environment']} ({detail})"


@auth_app.command()
def logout(
    ctx: typer.Context,
    environment: str | None = typer.Option(
        None,
        "--env",
        help=f"Log out of this environment: {_ENV_NAMES} "
        "(default: $ADE_ENV, then production).",
    ),
    all_envs: bool = typer.Option(
        False, "--all", help="Log out of every environment at once."
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Log out of one environment (the resolved target by default); `--all`
    clears every environment. Idempotent; OAuth refresh tokens are revoked
    best-effort first."""
    home = ade_home()
    ports: Ports = ctx.obj
    if environment is not None and all_envs:
        exit_with(
            {"error": "ambiguous_target", "message": "Pass --env or --all, not both."},
            "Pass --env or --all, not both.",
            as_json=as_json,
            code=EXIT_USAGE,
        )
    if environment is not None:
        validate_environment(environment, source="--env", as_json=as_json)
    if all_envs:
        revoked = oauth.revoke_stored(home, ports.transport)
        cleared = credentials.clear(home)
        _emit_logout(cleared, revoked, scope="all", environment=None, as_json=as_json)
        return
    target = resolve_target(home, environment, as_json=as_json).environment
    revoked = oauth.revoke_environment(home, target, ports.transport)
    cleared = credentials.clear_environment(home, target)
    _emit_logout(
        cleared, revoked, scope="environment", environment=target, as_json=as_json
    )


org_app = typer.Typer(
    name="org",
    no_args_is_help=True,
    help="Show or switch the organization an OAuth login acts in.",
)
auth_app.add_typer(org_app)


def _require_oauth_session(home, resolved: ResolvedConfig, *, as_json: bool) -> dict:
    """Org commands manage the *stored* OAuth session (ADE_API_KEY plays
    no part): exit with remediation when the target has none."""
    stored = credentials.stored_credential(home, resolved.environment)
    if stored is None:
        exit_with(
            {
                "error": "unauthenticated",
                "environment": resolved.environment,
                "message": f"Run `{resolved.login_hint}` first.",
            },
            f"Not authenticated for {resolved.endpoint_label}. Run "
            f"`{resolved.login_hint}` first.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    if stored.method != "oauth" or stored.oauth is None:
        exit_with(
            {
                "error": "org_requires_oauth",
                "environment": resolved.environment,
                "message": "Organization selection applies to browser "
                "(OAuth) logins; API keys are already organization-bound.",
            },
            "Organization selection applies to browser (OAuth) logins; the "
            f"stored credential for {resolved.environment} is an API key, "
            "which is already organization-bound. Log in with the browser "
            f"method first (`{resolved.login_hint}`).",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    return stored.oauth


def _fetch_orgs_or_exit(
    ports: Ports, home, resolved: ResolvedConfig, *, as_json: bool
) -> list[dict]:
    try:
        return oauth.fetch_organizations(home, resolved.environment, ports)
    except oauth.OrgDiscoveryError as error:
        exit_with(
            {
                "error": f"org_{error.reason}",
                "environment": resolved.environment,
                "message": error.message,
            },
            f"Could not list your organizations: {error.message}.",
            as_json=as_json,
            code=EXIT_FAILED,
        )


@org_app.command("list")
def org_list(
    ctx: typer.Context,
    environment: str | None = typer.Option(None, "--env", help=_ENV_HELP),
    as_json: bool = JSON_FLAG,
) -> None:
    """List the organizations the target's OAuth session can act in,
    marking the selected one. Memberships come live from the login
    provider, so a fresh grant or removal shows immediately — including a
    selection that is no longer a membership."""
    home = ade_home()
    ports: Ports = ctx.obj
    resolved = resolve_target(home, environment, as_json=as_json)
    entry = _require_oauth_session(home, resolved, as_json=as_json)
    organizations = _fetch_orgs_or_exit(ports, home, resolved, as_json=as_json)
    selected_id = ((entry.get("organization") or {}).get("id")) or None
    rows = [
        {**organization, "selected": organization["id"] == selected_id}
        for organization in organizations
    ]
    # A selection can outlive the membership behind it (removed from the
    # org, org deleted). The header keeps going out until it is changed,
    # and the platform rejects it — so say so instead of reporting a
    # default that is not what requests actually carry.
    stale = selected_id is not None and not any(row["selected"] for row in rows)
    lines = [f"{'*' if row['selected'] else ' '} {_org_label(row)}" for row in rows]
    if not rows:
        lines = ["Your account belongs to no organizations."]
    if stale:
        lines.append(
            f"Selected organization {selected_id} is no longer one of your "
            f"memberships: requests still send it and the platform will "
            f"reject them. Pick another with `{_org_switch_hint(resolved)}`, "
            f"or fall back to the platform default with "
            f"`{_org_clear_hint(resolved)}`."
        )
    elif selected_id is None:
        lines.append(
            "No organization selected; the platform default applies."
            + (f" Select one with `{_org_switch_hint(resolved)}`." if rows else "")
        )
    emit(
        {
            "environment": resolved.environment,
            "organizations": rows,
            "selected": selected_id,
            "selected_is_stale": stale,
        },
        "\n".join(lines),
        as_json=as_json,
    )


@org_app.command("switch")
def org_switch(
    ctx: typer.Context,
    org: str = typer.Argument(..., help="Organization id or name."),
    environment: str | None = typer.Option(None, "--env", help=_ENV_HELP),
    as_json: bool = JSON_FLAG,
) -> None:
    """Switch which organization the target's OAuth session acts in.
    Validated against your live memberships here, and membership-verified
    by the platform on every request regardless."""
    home = ade_home()
    ports: Ports = ctx.obj
    resolved = resolve_target(home, environment, as_json=as_json)
    entry = _require_oauth_session(home, resolved, as_json=as_json)
    previous = entry.get("organization") or None
    organizations = _fetch_orgs_or_exit(ports, home, resolved, as_json=as_json)
    organization = _match_org(organizations, org, resolved=resolved, as_json=as_json)
    oauth.set_organization(home, resolved.environment, organization)
    if previous and previous.get("id") == organization["id"]:
        human = (
            f"Already acting in {_org_label(organization)} for the "
            f"{resolved.environment} environment; nothing to do."
        )
    else:
        human = (
            f"Now acting in {_org_label(organization)} for the "
            f"{resolved.environment} environment."
        )
    emit(
        {
            "environment": resolved.environment,
            "organization": organization,
            "previous": previous,
            "stored": True,
        },
        human,
        as_json=as_json,
    )


@org_app.command("clear")
def org_clear(
    ctx: typer.Context,
    environment: str | None = typer.Option(None, "--env", help=_ENV_HELP),
    as_json: bool = JSON_FLAG,
) -> None:
    """Drop the target's organization selection, falling back to the
    platform default. Idempotent, and deliberately offline: this is the
    way out when a selection has outlived its membership, which is
    exactly when listing memberships may not work."""
    home = ade_home()
    resolved = resolve_target(home, environment, as_json=as_json)
    entry = _require_oauth_session(home, resolved, as_json=as_json)
    previous = entry.get("organization") or None
    oauth.set_organization(home, resolved.environment, None)
    human = (
        f"Cleared the organization selection for the {resolved.environment} "
        f"environment ({_org_label(previous)} → platform default)."
        if previous
        else f"No organization was selected for the {resolved.environment} "
        "environment; nothing to do."
    )
    emit(
        {
            "environment": resolved.environment,
            "organization": None,
            "previous": previous,
            "cleared": previous is not None,
        },
        human,
        as_json=as_json,
    )


def _emit_logout(
    cleared: bool, revoked: int, *, scope: str, environment: str | None, as_json: bool
) -> None:
    revoked_note = f" ({revoked} refresh token(s) revoked)" if revoked else ""
    if scope == "all":
        human = (
            f"Logged out of all environments{revoked_note}."
            if cleared
            else "No stored credentials; nothing to clear."
        )
    else:
        human = (
            f"Logged out of the {environment} environment{revoked_note}."
            if cleared
            else f"No stored credentials for the {environment} environment; "
            "nothing to clear."
        )
    emit(
        {
            "logged_out": True,
            "cleared": cleared,
            "revoked": revoked,
            "scope": scope,
            "environment": environment,
        },
        human,
        as_json=as_json,
    )
