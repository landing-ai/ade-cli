"""The ade command tree (ships as the ade-cli package)."""

from __future__ import annotations

import typer

from .auth import auth_app, login, logout
from .crop import crop
from .extract import extract
from .find import find
from .help import help_command
from .history import history_app
from .output import JSON_FLAG, emit, set_id_only
from .parse import parse
from .ports import Ports
from .telemetry import LedgerGroup
from .update import current_version, install_mode, update
from .view import view

app = typer.Typer(
    name="ade",
    cls=LedgerGroup,
    no_args_is_help=True,
    add_completion=False,
    help="CLI for Agentic Document Extraction (ADE) — parse and extract over "
    "the v2 document APIs, backed by a local job-item store.",
    # Bare `ade` prints this help, so the machine contract is surfaced
    # where an automated caller actually lands (F9) rather than only in
    # SKILL.md: one call for the whole surface, --json for every result.
    epilog="Automating ade? Run `ade help --json` once for the whole surface "
    "(commands, flags, result shapes, exit states), then pass --json to every "
    "command — the full result is always on stdout. `ade help workflow` "
    "explains how the verbs compose.",
)


app.add_typer(auth_app)
app.add_typer(history_app)
# Top-level aliases: the same callbacks as `auth login`/`auth logout`
# (identical flags and behavior), registered again at the root for muscle
# memory from other CLIs. Only the help line differs — it names the alias.
app.command(
    "login",
    help="Alias of `ade auth login`: ensure the target environment is "
    "logged in; --api-key authenticates with a key directly ('-' prompts "
    "with hidden input).",
)(login)
app.command(
    "logout",
    help="Alias of `ade auth logout`: log out of one environment (the "
    "resolved target by default); --all clears every environment.",
)(logout)
app.command()(parse)
app.command()(extract)
app.command()(find)
app.command()(view)
app.command()(crop)
app.command()(update)
app.command("help")(help_command)


@app.callback()
def _root(ctx: typer.Context) -> None:
    if ctx.obj is None:
        ctx.obj = Ports()
    # --id-only is a per-invocation output mode; the commands offering it
    # set it after this. Clearing here keeps a second in-process run (the
    # test seam, an embedding host) from inheriting the first one's mode.
    set_id_only(False)


@app.command()
def version(as_json: bool = JSON_FLAG) -> None:
    """Print the ade version and install mode: 'binary' (the standalone
    app — `ade update` replaces it in place) or 'python' (uv/pipx —
    upgrade with `uv tool upgrade ade-cli`)."""
    v = current_version()
    mode = install_mode()
    label = "standalone binary" if mode == "binary" else "python environment"
    emit(
        {"version": v, "install": mode},
        f"ade {v} ({label})",
        as_json=as_json,
    )
