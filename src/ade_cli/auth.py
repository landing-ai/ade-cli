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
"""

from __future__ import annotations

import os
import select
import sys

import httpx
import typer

from . import credentials, gateway, oauth, term
from .config import (
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
    environment: str | None = typer.Option(None, "--env", help=_ENV_HELP),
    as_json: bool = JSON_FLAG,
) -> None:
    """Ensure the target environment is logged in; `--api-key`
    authenticates with a key directly ('-' prompts with hidden input).
    Targets `--env`, else $ADE_ENV, else production — nothing is stored
    about the choice."""
    home = ade_home()
    resolved = resolve_target(home, environment, as_json=as_json)
    if api_key is None:
        _login_without_key(ctx.obj, home, resolved, as_json=as_json)
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

    Never blocks on an idle pipe: stdin is read only once select says a
    line is already there, so a harness that leaves stdin open but silent
    gets the remediation immediately instead of a hang. Where select
    cannot answer for the stream (Windows console handles) that is a No —
    those callers still have ``--api-key`` and ``ADE_API_KEY``.
    """
    if ports.stdin_is_tty():
        return None
    try:
        fileno = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        # Not a real OS stream (an in-process runner's buffer): readable
        # by construction, and reading it cannot block on a writer.
        fileno = None
    if fileno is not None:
        try:
            if not select.select([fileno], [], [], _PIPED_KEY_TIMEOUT)[0]:
                return None
        except OSError:
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
    ports: Ports, home, resolved: ResolvedConfig, *, as_json: bool
) -> None:
    """No credential flag: *ensure* logged in on the target. A stored
    credential means there is nothing to do (credentials are per
    environment; no selection exists to change). Otherwise acquire one —
    a terminal prompts (the ADR-0002 method menu); a non-interactive run
    takes a key piped on stdin, else the browser flow."""
    existing = credentials.stored_credential(home, resolved.environment)
    if existing is not None:
        _emit_already(home, resolved, existing, as_json=as_json)
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
        human = (
            f"Already authenticated for the {resolved.environment} environment "
            f"as {who}; nothing to do.\n{tail}"
        )
    else:
        human = (
            f"Already authenticated for the {resolved.environment} environment "
            f"({cred.method} {cred.masked}); nothing to do.\n{tail}"
        )
    emit(payload, human, as_json=as_json)


def _browser_login(
    ports: Ports, home, resolved: ResolvedConfig, *, as_json: bool
) -> None:
    entry = _run_browser_flow(ports, home, resolved, as_json=as_json)
    credentials.store_oauth(home, resolved.environment, entry)
    _emit_browser_login(home, resolved, entry, as_json=as_json)


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
    home, resolved: ResolvedConfig, entry: dict, *, as_json: bool
) -> None:
    identity = entry.get("identity") or {}
    who = identity.get("email") or identity.get("sub") or "unknown identity"
    emit(
        {
            "method": "oauth",
            "identity": identity,
            "credential": credentials.mask(entry["access_token"]),
            "stored": True,
            "environment": resolved.environment,
            "endpoint": resolved.endpoint,
            "endpoint_source": resolved.endpoint_source,
        },
        f"Logged in as {who} via browser (tokens stored in "
        f"{credentials.credentials_path(home)} for the "
        f"{resolved.environment} environment).\n"
        f"Endpoint: {resolved.endpoint} ({resolved.endpoint_source})",
        as_json=as_json,
    )


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
        payload.update(
            {
                "identity": identity,
                "expires_at": active.oauth.get("expires_at"),
                "expires_in_seconds": max(0, int(remaining)) if remaining is not None else None,
                "refresh_token": refresh == "available",
            }
        )
        who = identity.get("email") or identity.get("sub") or "unknown identity"
        human = (
            f"Authenticated via OAuth as {who} ({source_note}).\n"
            f"Access token {active.masked} {note}; refresh token {refresh}.\n"
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
