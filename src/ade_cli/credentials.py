"""Stored credentials: ``credentials.json`` under the store home, mode 0600,
separate from endpoint config. Keys are stored per environment — ADE keys
verifiably do not cross environments (a staging key 401s on dev) — under an
``environments`` map. The pre-environment schema (a single top-level
``api_key``) keeps being read as the currently-configured environment's
credential so existing installs don't break. Precedence: ``ADE_API_KEY``
env always wins over anything stored."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from .config import DEFAULT_ENVIRONMENT, ResolvedConfig
from .filelock import exclusive
from .output import EXIT_FAILED, exit_with

Method = Literal["api_key", "oauth"]
Source = Literal["env", "stored"]


def credentials_path(home: Path) -> Path:
    return home / "credentials.json"


@dataclass(frozen=True)
class ActiveCredential:
    method: Method
    secret: str  # the Bearer value: the API key, or the OAuth access token
    source: Source
    oauth: dict | None = None  # the stored OAuth entry (tokens, identity, expiry)

    @property
    def masked(self) -> str:
        return mask(self.secret)

    @property
    def org_id(self) -> str | None:
        """The OAuth session's selected organization id (ADR-0009), sent
        as ``x-org-id`` on API requests. None for API keys (already
        organization-bound) and for sessions with no selection (the
        platform default organization applies)."""
        if self.oauth is None:
            return None
        organization = self.oauth.get("organization")
        if isinstance(organization, dict):
            value = organization.get("id")
            if isinstance(value, str) and value:
                return value
        return None


def mask(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return "*" * 8 + secret[-4:]


def store_api_key(home: Path, key: str, environment: str) -> None:
    _store_entry(home, environment, {"method": "api_key", "api_key": key})


def store_oauth(home: Path, environment: str, entry: dict) -> None:
    """Persist an OAuth token set (access + refresh + identity + expiry).
    The most recent login wins: this replaces whatever method the
    environment held before."""
    _store_entry(home, environment, {"method": "oauth", "oauth": entry})


def _store_entry(home: Path, environment: str, entry: dict) -> None:
    stored = load_stored(home) or {}
    environments = dict(stored.get("environments") or {})
    if stored.get("method") == "api_key" and stored.get("api_key"):
        # Migrate the legacy single-credential schema on first write: it
        # predates named environments, when production — also the raw-
        # endpoint escape hatch's credential home — was the only namespace.
        environments.setdefault(
            DEFAULT_ENVIRONMENT, {"method": "api_key", "api_key": stored["api_key"]}
        )
    environments[environment] = entry
    _write_credentials(home, {"environments": environments})


def _write_credentials(home: Path, data: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = credentials_path(home)
    payload = json.dumps(data, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    os.chmod(path, 0o600)


@contextmanager
def refresh_lock(home: Path) -> Iterator[None]:
    """Cross-process guard for token refresh. Logto rotates refresh tokens,
    so two concurrent refreshes would each invalidate the other's new token;
    holders must re-read the stored entry after acquiring."""
    home.mkdir(parents=True, exist_ok=True)
    with exclusive(home / ".credentials.lock"):
        yield


def load_stored(home: Path) -> dict | None:
    path = credentials_path(home)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def oauth_entry(entry: object) -> dict | None:
    """The OAuth token set inside a stored environment entry, or None when
    the entry is another method, empty, or malformed."""
    if isinstance(entry, dict) and entry.get("method") == "oauth":
        oauth = entry.get("oauth")
        if isinstance(oauth, dict) and oauth.get("access_token"):
            return oauth
    return None


def clear(home: Path) -> bool:
    """Remove stored credentials (all environments). Returns whether any
    existed."""
    path = credentials_path(home)
    if path.exists():
        path.unlink()
        return True
    return False


def clear_environment(home: Path, environment: str) -> bool:
    """Remove one environment's stored credential (revocation happens
    separately). Returns whether one existed. Deletes the file entirely when
    nothing remains, so a single-environment logout leaves no empty artifact."""
    stored = load_stored(home)
    if not stored:
        return False
    environments = dict(stored.get("environments") or {})
    removed = environments.pop(environment, None) is not None
    rest = {k: v for k, v in stored.items() if k != "environments"}
    if (
        environment == DEFAULT_ENVIRONMENT
        and rest.get("method") == "api_key"
        and rest.get("api_key")
    ):
        # Legacy top-level credential is the production namespace.
        rest.pop("method", None)
        rest.pop("api_key", None)
        removed = True
    if not environments and not rest:
        clear(home)
        return removed
    _write_credentials(home, {**rest, "environments": environments})
    return removed


def _entry_credential(entry: object) -> ActiveCredential | None:
    """The stored credential inside one environment entry (or a legacy
    top-level dict), or None when it holds no usable credential."""
    if not isinstance(entry, dict):
        return None
    api_key = entry.get("api_key")
    if entry.get("method") == "api_key" and isinstance(api_key, str) and api_key:
        return ActiveCredential(method="api_key", secret=api_key, source="stored")
    oauth = oauth_entry(entry)
    if oauth is not None:
        return ActiveCredential(
            method="oauth", secret=oauth["access_token"], source="stored", oauth=oauth
        )
    return None


def stored_credential(home: Path, environment: str) -> ActiveCredential | None:
    """The credential stored for a specific environment, ignoring the
    ADE_API_KEY override. `login` uses this for its ensure short-circuit:
    a target that already holds a credential needs no re-authentication."""
    stored = load_stored(home) or {}
    cred = _entry_credential((stored.get("environments") or {}).get(environment))
    if cred is not None:
        return cred
    if environment == DEFAULT_ENVIRONMENT:
        # Legacy single-credential schema serves the production namespace.
        return _entry_credential(stored)
    return None


def stored_environments(home: Path) -> dict[str, ActiveCredential]:
    """Every environment that has a usable stored credential (ignores
    ADE_API_KEY). `status` lists these so authenticated targets are discoverable."""
    stored = load_stored(home) or {}
    result: dict[str, ActiveCredential] = {}
    for environment, entry in (stored.get("environments") or {}).items():
        cred = _entry_credential(entry)
        if cred is not None:
            result[environment] = cred
    if DEFAULT_ENVIRONMENT not in result:
        legacy = _entry_credential(stored)  # legacy top-level = production
        if legacy is not None:
            result[DEFAULT_ENVIRONMENT] = legacy
    return result


def resolve(home: Path, environment: str) -> ActiveCredential | None:
    env_key = os.environ.get("ADE_API_KEY")
    if env_key:
        return ActiveCredential(method="api_key", secret=env_key, source="env")
    return stored_credential(home, environment)


def require(home: Path, resolved: ResolvedConfig, *, as_json: bool) -> ActiveCredential:
    """The network verbs' auth gate: resolve or exit with remediation."""
    active = resolve(home, resolved.environment)
    if active is None:
        # Name --env for a non-default target so the hinted login
        # authenticates this exact environment.
        exit_with(
            {
                "error": "unauthenticated",
                "environment": resolved.environment,
                "message": f"Run `{resolved.login_hint}` or set ADE_API_KEY.",
            },
            f"Not authenticated for {resolved.endpoint_label}. Run "
            f"`{resolved.login_hint}` or set ADE_API_KEY.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    return active
