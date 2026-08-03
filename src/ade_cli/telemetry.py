"""Local usage ledger (#52): one event per invocation, never in the way.

Every command run — including store-served commands that never touch the
API — appends one JSON line to ``<ADE_HOME>/telemetry.jsonl``. The hook
is the root command group's ``main``: the one choke point every
invocation passes through (the installed script and the test runner
alike), so success, failure, and usage error all record — no per-command
wiring to drift.

Events carry names, never values: the command path and flag *names* come
from the raw argv classified against the registered command tree, so an
argument, a path, a URL, or a flag's value can never ride along — tokens
either match a registered command name or a ``-``/``--`` flag shape, or
they are dropped. Unresolvable invocations record ``(unknown)`` rather
than the attempted text (a typo could be a filename).

Telemetry must never change a command: the append is a single O_APPEND
write (atomic for lines this size), every failure is swallowed, and a
corrupt or unwritable ledger is invisible to the user. Opt-out disables
the ledger entirely: ``ADE_TELEMETRY=0`` or the ``DO_NOT_TRACK``
convention. Location, shape, and opt-out are documented in
docs/telemetry.md.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path
from typing import Mapping

from typer.core import TyperGroup

from . import surface
from .config import DEFAULT_ENVIRONMENT, ENVIRONMENTS
from .filelock import exclusive
from .output import EXIT_FAILED, EXIT_PENDING, EXIT_RATE_LIMITED, EXIT_USAGE

LEDGER_NAME = "telemetry.jsonl"
LEDGER_LOCK_NAME = ".telemetry.lock"

# Exit code → outcome, keyed by output.py's exit vocabulary itself so the
# two can never drift. Pending and rate-limited are normal outcomes there,
# so the ledger keeps them distinguishable rather than folding them into
# failure.
_OUTCOMES = {
    0: "success",
    EXIT_FAILED: "failure",
    EXIT_USAGE: "usage-error",
    EXIT_PENDING: "pending",
    EXIT_RATE_LIMITED: "rate-limited",
}


def ledger_path(home: Path) -> Path:
    return home / LEDGER_NAME


def ledger_lock_path(home: Path) -> Path:
    return home / LEDGER_LOCK_NAME


def enabled(env: Mapping[str, str]) -> bool:
    """ADE_TELEMETRY=0 and the DO_NOT_TRACK convention (any value but
    empty/0) both disable the ledger entirely."""
    if env.get("ADE_TELEMETRY") == "0":
        return False
    if env.get("DO_NOT_TRACK") not in (None, "", "0"):
        return False
    return True


def classify_argv(root: object, argv: list[str]) -> tuple[str, list[str]]:
    """(command path, flag names) from raw argv — names only, provably:
    a token is recorded only when it matches a registered command name
    while descending the tree, or a *declared* option name of a command
    on that path (name part only, values after ``=`` dropped). Everything
    else — arguments, paths, URLs, typos, and even values shaped like
    flags (``--model -sneaky``) — is skipped: undeclared tokens cannot
    ride into an event.

    Groups are recognized by their ``commands`` dict rather than a class
    (typer vendors click, so there is no stable Group type to name)."""
    candidates: list[str] = []
    path: list[str] = []
    # click appends the help option at parse time, so it is not in params.
    declared = {"--help"} | _declared_options(root)
    node: object | None = root
    positional = False  # saw a token that is neither command nor flag-shaped
    for index, token in enumerate(argv):
        if token == "--":
            # Everything after the separator is positional by definition.
            positional = positional or index + 1 < len(argv)
            break
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            if name not in candidates:
                candidates.append(name)
            continue
        children = getattr(node, "commands", None)
        if children is not None:
            child = children.get(token)
            if child is not None:
                path.append(token)
                node = child
                declared |= _declared_options(child)
                continue
        # A non-flag token that is not a subcommand: an argument (`-`
        # included — the stdin sentinel is positional). Stop descending
        # (nothing after it can be a command) but keep collecting flag
        # candidates.
        positional = True
        node = None
    flags = [name for name in candidates if name in declared]
    if path:
        return " ".join(path), flags
    return ("(unknown)" if positional else "(root)"), flags


def _declared_options(command: object) -> set[str]:
    """The option names a click command declares (``--json``, ``-d``, …) —
    the only tokens the ledger may record as flags."""
    names: set[str] = set()
    for param in getattr(command, "params", None) or []:
        for opts in (
            getattr(param, "opts", None) or [],
            getattr(param, "secondary_opts", None) or [],
        ):
            names.update(opt for opt in opts if opt.startswith("-"))
    return names


def _version() -> str:
    try:
        return _installed_version("ade-cli")
    except PackageNotFoundError:
        return "unknown"


# Endpoint URL → environment name, for mapping an ADE_ENDPOINT override
# back to the environment it actually targets.
_ENV_BY_URL = {url.rstrip("/"): name for name, url in ENVIRONMENTS.items()}


def _environment(argv: list[str], env: Mapping[str, str]) -> str:
    """The API target this invocation actually addresses, as a bounded
    vocabulary: an ADE_ENDPOINT override wins and is recorded by *where
    the traffic goes* — a known environment's URL maps back to its name,
    anything else records ``custom`` (the URL itself is a value and never
    recorded). Without an override, the resolved environment name
    (--env flag → ADE_ENV → production, resolve_target's precedence);
    a name outside the known set records ``unknown``, never the typed
    text. Note this is deliberately not meta.json's ``environment``
    field: that one is ResolvedConfig.environment (the credential and
    item-id namespace, unchanged by ADE_ENDPOINT) — the ledger segments
    by actual target instead. Never exits and never raises — an
    invocation the command will refuse loudly still records an event."""
    try:
        override = env.get("ADE_ENDPOINT")
        if override:
            return _ENV_BY_URL.get(override.rstrip("/"), "custom")
        name = None
        for index, token in enumerate(argv):
            if token == "--":
                break
            if token == "--env" and index + 1 < len(argv):
                name = argv[index + 1]
            elif token.startswith("--env="):
                name = token.split("=", 1)[1]
        if name is None:
            name = env.get("ADE_ENV") or None
        if name is None:
            return DEFAULT_ENVIRONMENT
        return name if name in ENVIRONMENTS else "unknown"
    except Exception:
        return "unknown"


def record_invocation(
    root: object,
    argv: list[str],
    *,
    exit_code: int,
    duration_seconds: float,
    env: Mapping[str, str],
    stdout_is_tty: bool,
) -> None:
    """Append one event, or silently nothing: opted out, unwritable,
    corrupt — telemetry never surfaces, never raises."""
    try:
        if not enabled(env):
            return
        home = Path(env["ADE_HOME"]) if env.get("ADE_HOME") else Path.home() / ".ade"
        detected = surface.detect(env, stdout_is_tty=stdout_is_tty)
        command, flags = classify_argv(root, argv)
        event = {
            "ts": time.time(),
            "version": _version(),
            "command": command,
            "flags": flags,
            "outcome": _OUTCOMES.get(exit_code, "failure"),
            "exit_code": exit_code,
            "duration_ms": max(0, int(duration_seconds * 1000)),
            "host": detected.host,
            "term": detected.term,
            "env": _environment(argv, env),
            # Minted once, at record time: the platform-side dedup handle
            # for at-least-once shipping (#53) — a re-uploaded event keeps
            # the same key, so a lost 200 never doubles it in analytics.
            "idempotent_key": uuid.uuid4().hex,
        }
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        home.mkdir(parents=True, exist_ok=True)
        # The ledger lock (shared with shipping.py's mark/rotate rewrite)
        # closes the append-vs-replace race: a rewrite can never strand
        # this append on a just-unlinked inode. Holders only ever touch
        # the file — never the network — so the wait is bounded in
        # milliseconds, and the O_APPEND single write stays as the
        # in-lock discipline.
        with exclusive(ledger_lock_path(home)):
            fd = os.open(
                ledger_path(home), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
            )
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
    except BaseException:
        # Not just Exception: a Ctrl-C landing inside this microsecond
        # window must not replace the command's real exit path either.
        pass


class LedgerGroup(TyperGroup):
    """The root command group with the ledger wrapped around ``main`` —
    the single entry point of every invocation, so exactly one event
    records per run whatever the outcome. Both the installed script and
    typer's test runner enter here."""

    def main(self, args=None, *pargs, **kwargs):  # type: ignore[override]
        argv = [str(a) for a in (args if args is not None else sys.argv[1:])]
        started = time.monotonic()
        exit_code = 0
        try:
            return super().main(args, *pargs, **kwargs)
        except SystemExit as e:
            # Standalone click wraps every outcome — including success —
            # in SystemExit; a non-int code is a message, which exits 1.
            if e.code is None:
                exit_code = 0
            elif isinstance(e.code, int):
                exit_code = e.code
            else:
                exit_code = 1
            raise
        except BaseException as error:
            # Non-standalone callers see click/typer exceptions directly;
            # those carry exit_code. Anything else is a crash (1).
            code = getattr(error, "exit_code", 1)
            exit_code = code if isinstance(code, int) else 1
            raise
        finally:
            record_invocation(
                self,
                argv,
                exit_code=exit_code,
                duration_seconds=time.monotonic() - started,
                env=os.environ,
                stdout_is_tty=_stdout_is_tty(),
            )
            # After the event is on disk, ship the unshipped backlog —
            # this invocation's event rides along (#53). Runs after the
            # command's output and exit path are already decided, and is
            # as silent as the append above.
            _ship_after_command(self, argv, kwargs.get("obj"))
            # Then the throttled update check (#138): same posture — after
            # the real work, never surfaces, never raises.
            _update_check_after_command(self, argv, kwargs.get("obj"))


def _ship_after_command(root: object, argv: list[str], ports: object) -> None:
    """Hand the post-command flush to shipping.py: the injected transport
    when the invocation carried Ports (the test seam), the real one
    otherwise. Never surfaces, never raises — same posture as the append."""
    try:
        from . import shipping

        transport = getattr(ports, "transport", None)
        if transport is None:
            import httpx

            transport = httpx.HTTPTransport()
        command, _ = classify_argv(root, argv)
        shipping.after_command(
            command=command, argv=argv, env=os.environ, transport=transport
        )
    except BaseException:
        pass


def _update_check_after_command(root: object, argv: list[str], ports: object) -> None:
    """Hand the post-command update check to update.py (#138): the
    injected transport and terminal-ness when the invocation carried
    Ports (the test seam), the real ones otherwise. Never surfaces,
    never raises — same posture as the flush above."""
    try:
        from . import update

        transport = getattr(ports, "transport", None)
        if transport is None:
            import httpx

            transport = httpx.HTTPTransport()
        stderr_is_tty = getattr(ports, "stderr_is_tty", _stderr_is_tty)()
        command, _ = classify_argv(root, argv)
        update.after_command(
            command=command,
            env=os.environ,
            transport=transport,
            stderr_is_tty=stderr_is_tty,
        )
    except BaseException:
        pass


def _stdout_is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _stderr_is_tty() -> bool:
    try:
        return sys.stderr.isatty()
    except Exception:
        return False
