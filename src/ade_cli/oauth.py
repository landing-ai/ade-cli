"""OAuth browser login (authorization-code + PKCE) against self-hosted Logto.

One Logto instance per environment; the ``login.*`` hostnames are the
canonical issuers (``logto.*`` is the admin portal). Provider settings are
data, not code: baked-in defaults per environment (issuer, resource) merged
under any ``oauth.<environment>`` block in ``config.json`` — which is also
how client ids arrive once the native-app registrations land. Endpoints
derive from the issuer by Logto's fixed shape (``{issuer}/auth``,
``/token``, ``/token/revocation``).

Logto specifics encoded here: ``prompt=consent`` + ``offline_access`` are
what earns a refresh token; refresh tokens rotate on every use (so refresh
holds a cross-process lock and re-reads before spending one); and a JWT
access token bound to the ADE API audience requires the RFC 8707
``resource`` indicator — without it Logto mints an opaque token. That
last rule is why organization discovery (ADR-0009) spends a refresh with
no resource: only the opaque token is welcome at userinfo, where the
organization claims live.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from . import credentials
from .config import ENVIRONMENTS, ResolvedConfig, load_config
from .gateway import BearerAuth, GatewayError, StaticBearer
from .ports import Clock, Ports

# urn:logto:scope:organizations makes userinfo return the user's
# organization memberships (the ``organizations`` / ``organization_data``
# claims) — the discovery behind the login org picker (ADR-0009).
SCOPES = "openid profile email offline_access urn:logto:scope:organizations"
LOGIN_TIMEOUT_SECONDS = 300.0
EXPIRY_SKEW_SECONDS = 60.0  # refresh this close to expiry, not at it
_POLL_TICK_SECONDS = 0.2
_TOKEN_TIMEOUT_SECONDS = 30.0

_ISSUERS = {
    "dev": "https://login.dev.landing.ai/oidc",
    "staging": "https://login.staging.landing.ai/oidc",
    "production": "https://login.landing.ai/oidc",
    "eu": "https://login.eu-west-1.landing.ai/oidc",
}

# Registered ade native-app client ids, per environment. Public
# identifiers, not secrets (native apps are public clients — PKCE, no
# client secret), so they live here as defaults: browser login must
# survive a wiped ~/.ade, and "config, not code" means config *can*
# override, not that config is required. Registered 2026-07-16 (dev) /
# 2026-07-22 (rest); provenance on issue #12.
_CLIENT_IDS = {
    "dev": "7zs0x5fjag7mhm6z4jbjh",
    "staging": "ajuo8ch2yle7xu8fvsz3c",
    "production": "a7k31qip5bylclf3kfgdg",
    "eu": "3i9hgicjpdh0ibsiq3ri4",
}


@dataclass(frozen=True)
class Provider:
    issuer: str
    client_id: str | None  # None until the environment's registration lands
    resource: str | None
    redirect_port: int  # 0 = OS-assigned (RFC 8252); pin it if Logto insists

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.issuer}/auth"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/token"

    @property
    def revocation_endpoint(self) -> str:
        return f"{self.issuer}/token/revocation"

    @property
    def userinfo_endpoint(self) -> str:
        return f"{self.issuer}/me"


def resolve_provider(home: Path, environment: str) -> Provider:
    """Defaults for the known environments, field-by-field overridable from
    the ``oauth.<environment>`` block in config.json. The default resource
    indicator is the environment's own API endpoint (the token audience)."""
    override = (load_config(home).get("oauth") or {}).get(environment) or {}
    return Provider(
        issuer=(override.get("issuer") or _ISSUERS.get(environment) or "").rstrip("/"),
        client_id=override.get("client_id") or _CLIENT_IDS.get(environment),
        resource=override.get("resource") or ENVIRONMENTS.get(environment),
        redirect_port=int(override.get("redirect_port") or 0),
    )


class LoginError(Exception):
    """The browser flow ended without tokens; ``reason`` is machine-readable."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class BrowserUnavailable(LoginError):
    def __init__(self) -> None:
        super().__init__("no_browser", "could not open a browser")


class _TokenEndpointError(Exception):
    """Non-200 from the token endpoint; callers translate it into their
    flow's failure (LoginError at login, ReloginRequired at refresh)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"token endpoint answered {status_code}: {detail}")
        self.detail = detail


class ReloginRequired(GatewayError):
    """An OAuth session this CLI cannot repair; re-login is the remediation.
    A GatewayError so the guarantee loop surfaces it like any HTTP failure."""

    def __init__(self, why: str):
        super().__init__(401, f"OAuth session unusable ({why}). Run `ade auth login`.")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _CallbackServer:
    """Loopback listener for the authorization redirect (RFC 8252 §7.3).
    Binds 127.0.0.1 on an OS-assigned port unless the provider pins one.

    Only a callback carrying this login attempt's ``state`` is accepted —
    anything else (stray localhost probes, a forged redirect) gets a 400
    and the flow keeps waiting, so an attacker who can reach the port can
    neither complete nor kill the login."""

    def __init__(self, port: int, state: str):
        self.result: dict[str, str] | None = None
        outer = self
        # The landing page shown in the browser tab — the LandingAI-branded
        # template (see auth_callback.html for its design provenance).
        template = (
            resources.files("ade_cli")
            .joinpath("auth_callback.html")
            .read_text("utf-8")
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server contract)
                url = urlsplit(self.path)
                if url.path != "/callback":
                    self.send_error(404)
                    return
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if query.get("state") != state:
                    self.send_error(400, "unexpected request")
                    return
                if outer.result is None:  # first response wins
                    outer.result = query
                if query.get("error"):
                    # e.g. access_denied. The description is IdP-supplied
                    # text riding the redirect — escaped like any untrusted
                    # value (state matched, but never render raw).
                    variant, headline = "error", "Sign-in didn't complete"
                    detail = (
                        (query.get("error_description") or query["error"])
                        + " — you can close this tab and return to the "
                        "terminal for details."
                    )
                else:
                    variant, headline = "success", "You're signed in to"
                    detail = "You can close this tab and return to the terminal."
                body = (
                    template.replace("__VARIANT__", variant)
                    .replace("__HEADLINE__", escape(headline))
                    .replace("__DETAIL__", escape(detail))
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass  # never write access logs over the CLI's own output

        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/callback"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def login(provider: Provider, ports: Ports, *, timeout: float = LOGIN_TIMEOUT_SECONDS) -> dict:
    """Run the browser flow; return the credential entry to store.

    Raises LoginError — BrowserUnavailable for the headless case, reasons
    ``denied`` / ``timeout`` / ``state_mismatch`` / ``token_endpoint`` for
    the others. Never stores anything itself.
    """
    assert provider.client_id, "caller checks configuration before starting the flow"
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))
    server = _CallbackServer(provider.redirect_port, state)
    redirect_uri = server.redirect_uri
    try:
        params = {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # Logto issues a refresh token only under an explicit consent
            # prompt (offline_access alone is not enough).
            "prompt": "consent",
        }
        if provider.resource:
            params["resource"] = provider.resource
        if not ports.browser(f"{provider.authorization_endpoint}?{urlencode(params)}"):
            raise BrowserUnavailable()
        deadline = ports.clock.monotonic() + timeout
        while server.result is None:
            if ports.clock.monotonic() >= deadline:
                raise LoginError(
                    "timeout", f"browser login timed out after {timeout:.0f}s"
                )
            ports.clock.sleep(_POLL_TICK_SECONDS)
        result = server.result
    finally:
        server.close()
    # The listener only admits state-matched callbacks, so both branches
    # below are answers from this login attempt's authorization server.
    if "error" in result:
        raise LoginError(
            "denied", result.get("error_description") or result["error"]
        )
    code = result.get("code")
    if not code:
        raise LoginError("no_code", "authorization response carried no code")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
        "code_verifier": verifier,
    }
    if provider.resource:
        data["resource"] = provider.resource
    try:
        payload = _post_token(ports.transport, provider, data)
    except _TokenEndpointError as error:
        raise LoginError("token_endpoint", str(error)) from error
    return _entry_from(payload, clock=ports.clock, previous=None)


def _post_token(
    transport: httpx.BaseTransport, provider: Provider, data: dict
) -> dict:
    with httpx.Client(transport=transport, timeout=_TOKEN_TIMEOUT_SECONDS) as client:
        response = client.post(provider.token_endpoint, data=data)
    if response.status_code != 200:
        try:
            body = response.json()
            detail = body.get("error_description") or body.get("error") or response.text
        except (json.JSONDecodeError, AttributeError):
            detail = response.text
        raise _TokenEndpointError(response.status_code, str(detail))
    # A 200 whose body is not a JSON object is still a broken answer, and
    # every caller translates _TokenEndpointError into its own flow's
    # failure — so normalizing here is what keeps a malformed response
    # from escaping as a decode error nobody catches.
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _TokenEndpointError(200, f"response body was not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise _TokenEndpointError(
            200, f"response body was {type(payload).__name__}, not a JSON object"
        )
    return payload


def _entry_from(payload: dict, *, clock: Clock, previous: dict | None) -> dict:
    access_token = payload.get("access_token")
    if not access_token:
        raise _TokenEndpointError(200, "response carried no access_token")
    expires_in = float(payload.get("expires_in") or 3600.0)
    entry = {
        "access_token": access_token,
        # Rotation: Logto invalidates the spent refresh token; when a
        # response omits a new one (spec-legal), the previous stays valid.
        "refresh_token": payload.get("refresh_token")
        or (previous or {}).get("refresh_token"),
        "expires_at": clock.now() + expires_in,
        "identity": _identity(payload) or (previous or {}).get("identity") or {},
    }
    # The org selection is CLI state, never in a token response — it
    # survives every refresh until set_organization changes it.
    organization = (previous or {}).get("organization")
    if organization:
        entry["organization"] = organization
    return entry


def _identity(payload: dict) -> dict | None:
    """Claims from the id_token, decoded without signature verification —
    it arrived straight from the issuer over TLS, and nothing downstream
    trusts it for authorization (the access token is the credential)."""
    id_token = payload.get("id_token")
    if not id_token:
        return None
    try:
        body = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        return None
    return {k: claims[k] for k in ("sub", "email", "name") if claims.get(k)}


class OrgDiscoveryError(Exception):
    """Organization discovery or selection failed; ``reason`` is
    machine-readable. Never raised by the login flow itself — callers
    decide whether a failure blocks (an explicit --org) or degrades to
    the platform default (the automatic post-login pick)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def fetch_organizations(home: Path, environment: str, ports: Ports) -> list[dict]:
    """The stored session's Logto organization memberships, as
    ``[{"id", "name"}, ...]`` from the userinfo claims (ADR-0009).

    The stored access token is audience-bound to the ADE API (RFC 8707),
    which userinfo rejects, so this spends one refresh WITHOUT the
    resource indicator to mint a userinfo-capable token. The rotated
    refresh token is persisted under the cross-process lock; the stored
    API access token (still live) is left untouched. Raises
    OrgDiscoveryError; never changes the stored selection."""
    provider = resolve_provider(home, environment)
    if not provider.client_id:
        raise OrgDiscoveryError(
            "not_configured", f"OAuth is not configured for {environment}"
        )
    with credentials.refresh_lock(home):
        stored = credentials.load_stored(home) or {}
        entry = credentials.oauth_entry(
            (stored.get("environments") or {}).get(environment)
        )
        if entry is None:
            raise OrgDiscoveryError(
                "no_session", "no OAuth session is stored for this environment"
            )
        refresh_token = entry.get("refresh_token")
        if not refresh_token:
            raise OrgDiscoveryError(
                "no_refresh_token", "the stored session has no refresh token"
            )
        try:
            payload = _post_token(
                ports.transport,
                provider,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": provider.client_id,
                },
            )
        except _TokenEndpointError as error:
            raise OrgDiscoveryError("token_endpoint", str(error)) from error
        except httpx.HTTPError as error:
            raise OrgDiscoveryError(
                "unreachable", f"could not reach the login provider ({error})"
            ) from error
        if payload.get("refresh_token"):
            credentials.store_oauth(
                home,
                environment,
                {**entry, "refresh_token": payload["refresh_token"]},
            )
    userinfo_token = payload.get("access_token")
    if not userinfo_token:
        raise OrgDiscoveryError(
            "token_endpoint", "refresh response carried no access_token"
        )
    try:
        with httpx.Client(
            transport=ports.transport, timeout=_TOKEN_TIMEOUT_SECONDS
        ) as client:
            response = client.get(
                provider.userinfo_endpoint,
                headers={"Authorization": f"Bearer {userinfo_token}"},
            )
    except httpx.HTTPError as error:
        raise OrgDiscoveryError(
            "unreachable", f"could not reach the login provider ({error})"
        ) from error
    if response.status_code != 200:
        raise OrgDiscoveryError(
            "userinfo", f"userinfo answered {response.status_code}"
        )
    try:
        claims = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise OrgDiscoveryError(
            "userinfo", f"userinfo body was not JSON: {error}"
        ) from error
    if not isinstance(claims, dict):
        raise OrgDiscoveryError(
            "userinfo",
            f"userinfo body was {type(claims).__name__}, not a JSON object",
        )
    if "organizations" not in claims:
        # The refresh token predates the organizations scope: userinfo
        # omits the claim entirely (an empty membership list would be []).
        raise OrgDiscoveryError(
            "relogin_required",
            "this session predates organization support — log out and back in",
        )
    memberships = claims.get("organizations") or []
    if not isinstance(memberships, list):
        raise OrgDiscoveryError(
            "userinfo", "userinfo carried a malformed organizations claim"
        )
    named = {
        item.get("id"): item
        for item in claims.get("organization_data") or []
        if isinstance(item, dict) and item.get("id")
    }
    return [
        {"id": org_id, "name": (named.get(org_id) or {}).get("name") or org_id}
        for org_id in memberships
        if isinstance(org_id, str) and org_id
    ]


def set_organization(home: Path, environment: str, organization: dict | None) -> None:
    """Persist the org selection on the stored OAuth session (None
    clears it). The selection rides ``x-org-id`` on API requests, where
    the platform verifies membership per request (ADR-0009)."""
    with credentials.refresh_lock(home):
        stored = credentials.load_stored(home) or {}
        entry = credentials.oauth_entry(
            (stored.get("environments") or {}).get(environment)
        )
        if entry is None:
            raise OrgDiscoveryError(
                "no_session", "no OAuth session is stored for this environment"
            )
        entry = dict(entry)
        if organization is None:
            entry.pop("organization", None)
        else:
            entry["organization"] = {
                "id": organization["id"],
                "name": organization.get("name") or organization["id"],
            }
        credentials.store_oauth(home, environment, entry)


def revoke_stored(home: Path, transport: httpx.BaseTransport) -> int:
    """Best-effort revocation of every stored refresh token before logout
    clears them; returns how many the server confirmed. Failures never
    block logout — clearing the local file is the guarantee."""
    stored = credentials.load_stored(home) or {}
    revoked = 0
    for environment, entry in (stored.get("environments") or {}).items():
        revoked += _revoke_entry(home, environment, entry, transport)
    return revoked


def revoke_environment(
    home: Path, environment: str, transport: httpx.BaseTransport
) -> int:
    """Best-effort revocation of one environment's refresh token; returns
    how many the server confirmed (0 or 1)."""
    stored = credentials.load_stored(home) or {}
    entry = (stored.get("environments") or {}).get(environment)
    return _revoke_entry(home, environment, entry, transport)


def _revoke_entry(
    home: Path, environment: str, entry: object, transport: httpx.BaseTransport
) -> int:
    oauth = credentials.oauth_entry(entry)
    refresh_token = (oauth or {}).get("refresh_token")
    provider = resolve_provider(home, environment)
    if not refresh_token or not provider.client_id:
        return 0
    try:
        with httpx.Client(transport=transport, timeout=_TOKEN_TIMEOUT_SECONDS) as client:
            response = client.post(
                provider.revocation_endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": provider.client_id,
                },
            )
        return 1 if response.status_code == 200 else 0
    except httpx.HTTPError:
        return 0  # best-effort by contract; the local clear still happens


@dataclass
class OAuthSession:
    """Bearer source for OAuth credentials (the gateway's BearerAuth):
    proactive refresh near expiry, forced refresh after a 401, rotated
    refresh tokens persisted under the cross-process lock."""

    home: Path
    environment: str
    transport: httpx.BaseTransport
    clock: Clock
    _issued: str | None = None  # the token this process last handed out

    def token(self) -> str:
        entry = self._entry()
        if self._expiring(entry):
            return self._refresh()
        self._issued = entry["access_token"]
        return self._issued

    def retry_after_401(self) -> str:
        return self._refresh()

    def _entry(self) -> dict:
        stored = credentials.load_stored(self.home) or {}
        entry = credentials.oauth_entry(
            (stored.get("environments") or {}).get(self.environment)
        )
        if entry is None:
            raise ReloginRequired("stored login is gone")
        return entry

    def _expiring(self, entry: dict) -> bool:
        expires_at = entry.get("expires_at")
        if expires_at is None:
            return False
        return self.clock.now() >= float(expires_at) - EXPIRY_SKEW_SECONDS

    def _refresh(self) -> str:
        with credentials.refresh_lock(self.home):
            entry = self._entry()
            # Another process may have refreshed while we waited on the
            # lock; a live token that isn't the one we handed out is theirs.
            if not self._expiring(entry) and entry["access_token"] != self._issued:
                self._issued = entry["access_token"]
                return self._issued
            refresh_token = entry.get("refresh_token")
            if not refresh_token:
                raise ReloginRequired("no refresh token was issued")
            provider = resolve_provider(self.home, self.environment)
            if not provider.client_id:
                raise ReloginRequired(
                    f"OAuth is not configured for {self.environment}"
                )
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": provider.client_id,
            }
            if provider.resource:
                data["resource"] = provider.resource
            try:
                payload = _post_token(self.transport, provider, data)
            except _TokenEndpointError as error:
                raise ReloginRequired(f"refresh failed: {error.detail}") from error
            entry = _entry_from(payload, clock=self.clock, previous=entry)
            credentials.store_oauth(self.home, self.environment, entry)
            self._issued = entry["access_token"]
            return self._issued


def bearer_auth(
    home: Path,
    resolved: ResolvedConfig,
    active: credentials.ActiveCredential,
    ports: Ports,
) -> BearerAuth:
    """The gateway's auth for whatever credential is active: OAuth sessions
    refresh; API keys (stored or env) are static."""
    if active.method == "oauth":
        return OAuthSession(
            home=home,
            environment=resolved.environment,
            transport=ports.transport,
            clock=ports.clock,
        )
    return StaticBearer(active.secret)
