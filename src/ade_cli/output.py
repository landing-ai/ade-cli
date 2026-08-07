"""Output convention: human-readable by default; ``--json`` emits one
stable JSON object/array on stdout. Errors and pending payloads follow
the same rule.

A third mode, ``--id-only``, prints just the id(s) a run produced — the
piping spelling of the same payload (``JOB=$(ade parse -d f.pdf
--id-only)``). It is a whole-output mode, not a payload field, so it
applies here at the one funnel every command emits through: results
reduce to their ids on stdout, and anything that isn't a result — errors,
remediation — goes to stderr instead, so a captured id is never a
sentence. Pending payloads carry the id too, which is what makes
submit-and-return (``--wait 0 --id-only``) a one-liner."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

import typer
# typer vendors its click fork (no top-level `click` ships with it);
# these are the concrete exception types its validation layer raises.
from typer._click.exceptions import (
    BadParameter,
    MissingParameter,
    NoSuchOption,
    UsageError,
)

JSON_FLAG = typer.Option(False, "--json", help="Emit one stable JSON object on stdout.")
ID_ONLY_FLAG = typer.Option(
    False,
    "--id-only",
    help="Print only the id(s) this run produced, one per line — the "
    "piping mode (JOB=$(ade parse -d f.pdf `--id-only`)). Takes precedence "
    "over `--json`; errors and hints go to stderr.",
)

# Machine-readable exit states, shared by every command: pending is a normal
# outcome distinct from failure; usage means the invocation itself was wrong.
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PENDING = 3
EXIT_RATE_LIMITED = 4


def tilde(path: Path | str) -> str:
    """Abbreviate the home-directory prefix as ``~`` for human lines only;
    machine payloads always carry the full path.

    Path-based (not string-prefix) so it works whatever the platform's
    separator is. Anything that isn't under home — including URL sources,
    which ``Path()`` would mangle (``//`` collapses) — passes through as
    the exact string it came in as."""
    text = str(path)
    try:
        relative = Path(text).relative_to(Path.home())
    except ValueError:
        return text
    return str(Path("~") / relative)


def timestamp(epoch: float | None) -> str:
    """Render a stored epoch as the compact UTC form human summaries use.
    Items recorded before the field existed read as unknown, never crash
    a summary that is otherwise servable."""
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


# Whole-output mode, set per invocation by the commands offering
# --id-only and reset by the root callback before every run (one process
# runs one command; the reset keeps in-process callers hermetic).
_id_only = False


def set_id_only(enabled: bool) -> None:
    global _id_only
    _id_only = enabled


def payload_ids(payload: Any) -> list[str]:
    """The id(s) a success payload produced, in output order: the element
    ids of a match list (``find``), else the job item id of a record. A
    payload with no id at all prints nothing — an empty result set is
    empty output, never a message on stdout."""
    if isinstance(payload, dict):
        value = payload.get("job_item_id")
        return [value] if isinstance(value, str) else []
    if isinstance(payload, list):
        found = []
        for record in payload:
            if not isinstance(record, dict):
                continue
            value = record.get("element_id") or record.get("job_item_id")
            if isinstance(value, str):
                found.append(value)
        return found
    return []


def emit(payload: Any, human: str, *, as_json: bool) -> None:
    if _id_only:
        # Errors keep their remediation — on stderr, where it cannot
        # contaminate a captured id.
        if isinstance(payload, dict) and "error" in payload:
            typer.echo(human, err=True)
            return
        for value in payload_ids(payload):
            typer.echo(value)
        return
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(human)


def click_usage_payload(error: UsageError) -> dict:
    """One machine payload for a Click validation failure — the ``--json``
    contract extended to errors raised before any command body runs
    (#155): a stable ``error`` code plus the same message Click renders.
    The generic code is ``usage``; the recognizable subtypes get their
    own so scripts can branch without parsing the message."""
    payload: dict = {"error": "usage", "message": error.format_message()}
    param = getattr(error, "param", None)
    spelled = None
    if param is not None:
        opts = [*getattr(param, "opts", []), *getattr(param, "secondary_opts", [])]
        spelled = ", ".join(opts) or getattr(param, "name", None)
    if isinstance(error, NoSuchOption):
        payload["error"] = "no_such_option"
        payload["option"] = error.option_name
    elif isinstance(error, MissingParameter):
        kind = getattr(param, "param_type_name", "parameter")
        payload["error"] = (
            "missing_option" if kind == "option" else "missing_argument"
        )
        if spelled:
            payload["param"] = spelled
    elif isinstance(error, BadParameter):
        payload["error"] = "bad_parameter"
        if spelled:
            payload["param"] = spelled
    # typer's vendored path validation formats the offending path with
    # repr(), doubling every backslash on Windows (#172) — undo it for
    # path-typed params so the message carries the path as typed. (UNC
    # prefixes come back right too: repr's \\\\server\\share collapses to
    # \\server\share.)
    kind = getattr(getattr(param, "type", None), "name", "")
    if kind in ("path", "file", "filename", "directory"):
        payload["message"] = payload["message"].replace("\\\\", "\\")
    return payload


def exit_with(payload: dict, human: str, *, as_json: bool, code: int) -> NoReturn:
    """Terminal outcome that isn't the command's success — includes pending,
    which is a normal outcome, not an error."""
    emit(payload, human, as_json=as_json)
    raise typer.Exit(code=code)
