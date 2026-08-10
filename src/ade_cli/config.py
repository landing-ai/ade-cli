"""Store home and per-invocation target resolution.

The API target is resolved fresh on every invocation — never stored
(ADR-0003, superseding ADR-0001's stored selection): the ``--env`` flag
wins, then the ``ADE_ENV`` variable (shell-scoped stickiness — two
terminals can work two environments at once), then production, the
stable default. ``ADE_ENDPOINT`` overrides the endpoint URL alone (the
raw escape hatch); credentials still file under the resolved
environment. ``config.json`` holds the ``oauth.<environment>`` provider
overrides (see oauth.py), the ``update_check`` opt-out (update.py), and
the schema-handoff keys (schema_build.py): ``agent`` — a remembered
``{"default": "claude"}`` menu choice or a custom
``{"name": ..., "command": [...]}`` launcher — and ``schema_prompt:
false`` to silence the post-parse offer. ``ADE_HOME`` relocates the
store.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .output import EXIT_USAGE, exit_with
from .store import write_atomic

ENVIRONMENTS = {
    "dev": "https://api.ade.dev.landing.ai",
    "staging": "https://api.ade.staging.landing.ai",
    "production": "https://api.ade.landing.ai",
    "eu": "https://api.ade.eu-west-1.landing.ai",
}
DEFAULT_ENVIRONMENT = "production"
DEFAULT_ENDPOINT = ENVIRONMENTS[DEFAULT_ENVIRONMENT]

_ENV_NAMES = ", ".join(ENVIRONMENTS)

# "env" is the ADE_ENDPOINT override; "environment" a named target
# (--env or ADE_ENV); "default" the flagless production fallback.
EndpointSource = Literal["env", "environment", "default"]


def ade_home() -> Path:
    env = os.environ.get("ADE_HOME")
    return Path(env) if env else Path.home() / ".ade"


def config_path(home: Path) -> Path:
    return home / "config.json"


def load_config(home: Path) -> dict:
    path = config_path(home)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_config(home: Path, config: dict) -> None:
    """Publish the whole config atomically (temp-write + rename, the
    shared-file posture). Callers treat failure as advisory — remembering
    a preference is never worth failing the command that noticed it."""
    home.mkdir(parents=True, exist_ok=True)
    write_atomic(config_path(home), json.dumps(config, indent=2) + "\n")


@dataclass(frozen=True)
class ResolvedConfig:
    """The endpoint in effect plus the environment whose credentials apply
    — for this one invocation. ``environment`` is always a name: the
    ``--env``/``ADE_ENV`` target, or production when neither names one —
    including under ``ADE_ENDPOINT``, whose credentials file under it.
    """

    endpoint: str
    endpoint_source: EndpointSource
    environment: str

    @property
    def endpoint_label(self) -> str:
        """How messages name the API target: by environment when one
        determined the endpoint, by raw URL otherwise."""
        if self.endpoint_source in ("environment", "default"):
            return f"the {self.environment} environment ({self.endpoint})"
        return self.endpoint

    @property
    def login_hint(self) -> str:
        """The `auth login` invocation that authenticates *this* target:
        --env for a non-default environment, bare otherwise."""
        if self.environment != DEFAULT_ENVIRONMENT:
            return f"ade auth login --env {self.environment}"
        return "ade auth login"


def validate_environment(environment: str, *, source: str, as_json: bool) -> None:
    """Refuse an unknown environment name loudly, naming where it came
    from (the --env flag or the ADE_ENV variable) and the known choices."""
    if environment not in ENVIRONMENTS:
        exit_with(
            {
                "error": "unknown_environment",
                "environment": environment,
                "source": source,
                "known": sorted(ENVIRONMENTS),
            },
            f"Unknown environment {environment!r} (from {source}): choose "
            f"from {_ENV_NAMES}, or set ADE_ENDPOINT for a raw URL.",
            as_json=as_json,
            code=EXIT_USAGE,
        )


def resolve_target(
    home: Path, environment: str | None, *, as_json: bool
) -> ResolvedConfig:
    """One resolution rule for every command: ``--env`` flag → ``ADE_ENV``
    → production, with ``ADE_ENDPOINT`` overriding the endpoint URL only.
    (``home`` is unused today but keeps the seam stable for config-driven
    resolution such as oauth overrides living beside it.)"""
    source = "--env"
    if environment is None:
        ambient = os.environ.get("ADE_ENV")
        if ambient:
            environment, source = ambient, "ADE_ENV"
    named = environment is not None
    if environment is not None:
        validate_environment(environment, source=source, as_json=as_json)
    environment = environment or DEFAULT_ENVIRONMENT
    override = os.environ.get("ADE_ENDPOINT")
    if override:
        return ResolvedConfig(override.rstrip("/"), "env", environment)
    return ResolvedConfig(
        ENVIRONMENTS[environment], "environment" if named else "default", environment
    )
