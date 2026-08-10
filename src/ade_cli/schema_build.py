"""``schema build`` — hand a parsed document to the build-schema skill.

The schema between ``parse`` and ``extract`` is the one artifact this CLI
cannot produce alone: the build-schema skill (the landingai-ade plugin of
landing-ai/claude-skills) authors it inside an AI-agent session that
drives this very CLI — real extractions, reviewer-built ground truth,
converged-or-blocked stops. This module owns the bridge, in both
directions:

- ``ade schema build JOB_ITEM_ID`` launches an agent session (claude,
  codex, or a config-declared CLI — see agents.py) seeded with the parse
  item, the user's extraction intent, and a workspace; when the session
  ends it looks for the skill's ``deliverable/schema.json`` and offers to
  run the extract.
- ``offer_after_parse`` is the post-parse doorway into the same flow,
  heavily gated: a real terminal on all three streams, no ``--json`` or
  ``--id-only``, no agent host or CI (an agent driving ``ade parse`` must
  never meet a menu), and not silenced by ``schema_prompt: false``.

ade itself stays network-free here: agents are found on PATH, the skill
is acquired by the agent (the seeded prompt says how), and the extract
re-run is a re-exec of this CLI. Everything interactive rides stderr;
stdout keeps the one-payload contract.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import typer

from . import agents, items, store, surface, term
from .config import DEFAULT_ENVIRONMENT, ade_home, load_config, save_config
from .history import resolve_or_exit
from .output import EXIT_FAILED, EXIT_USAGE, JSON_FLAG, emit, exit_with, tilde
from .ports import Ports
from .update import reexec_argv

schema_app = typer.Typer(
    name="schema",
    help="Schema authoring: build an extraction schema from a parsed "
    "document with an AI-agent session running the build-schema skill.",
    no_args_is_help=True,
)


def _slug(source: str) -> str:
    """A filesystem-friendly engagement slug from the parse source (a
    path or URL): the filename stem, lowercased, squeezed to [a-z0-9-]."""
    stem = Path(source.split("?", 1)[0].rstrip("/")).stem
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "document"


def _readable_config(home: Path) -> dict:
    """config.json, or an empty one when it cannot be read. Everything
    this module takes from it — the agent preference, the prompt opt-out
    — is optional, so a corrupt or unreadable file must cost the
    preference, never the handoff (and never a completed parse)."""
    try:
        return load_config(home)
    except (OSError, ValueError):
        return {}


def _parse_item_or_exit(
    jobs: store.JobStore, token: str, *, as_json: bool
) -> tuple[str, dict]:
    """Resolve TOKEN to a completed parse item and return ``(item_id,
    meta)`` — the same gate (and error shapes) extract's JOB_ITEM_ID form
    applies, since the skill will extract against this item."""
    item_id = resolve_or_exit(jobs, token, as_json=as_json)
    record = items.item_record(jobs, item_id)
    if record["kind"] != "parse":
        exit_with(
            {
                "error": "not_a_parse_item",
                "job_item_id": item_id,
                "kind": record["kind"],
                "message": "schema build takes a parse job item id.",
            },
            f"Job item {item_id} is an extract item; schema build takes a "
            "parse job item id (run `ade history list`).",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    live = items.live_parse(jobs, item_id)
    if live is None:
        exit_with(
            {
                "error": "not_parsed",
                "job_item_id": item_id,
                "state": record["state"],
                "message": "No completed parse; re-run `ade parse` to finish it.",
            },
            f"Job item {item_id} has no completed parse "
            f"(state: {record['state']}); re-run `ade parse` to finish it.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    meta, _response = live
    return item_id, meta


def _choose_agent(
    ports: Ports,
    home: Path,
    found: list[agents.Agent],
    *,
    as_json: bool,
) -> agents.Agent:
    """One launchable agent out of several: the arrow-key menu on a real
    terminal (ADR-0002's pattern), a typed numbered fallback otherwise —
    and under ``--json``, where no prompt may fire, a usage error naming
    ``--agent``. The choice is remembered in config.json so the menu asks
    once per machine, not once per document."""
    if as_json:
        exit_with(
            {
                "error": "agent_ambiguous",
                "agents": [agent.name for agent in found],
                "message": "Several agent CLIs are available; pass --agent.",
            },
            "Several agent CLIs are available "
            f"({', '.join(agent.name for agent in found)}); pass --agent.",
            as_json=as_json,
            code=EXIT_USAGE,
        )
    labels = [f"{i + 1}) {agent.name}" for i, agent in enumerate(found)]
    index: int | None = None
    if ports.stdin_is_tty() and os.environ.get("TERM") != "dumb":
        typer.echo("Which agent should build the schema? (↑/↓ and Enter)", err=True)
        try:
            index = term.select(labels, getchar=ports.getchar)
        except term.Unsupported:
            pass  # the widget erased itself; the typed fallback re-lists
    else:
        typer.echo("Which agent should build the schema?", err=True)
    if index is None:
        for label in labels:
            typer.echo(f"  {label}", err=True)
        while True:
            choice = typer.prompt("Agent", default="1", err=True).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(found):
                index = int(choice) - 1
                break
            typer.echo(f"Choose 1-{len(found)}.", err=True)
    chosen = found[index]
    _remember_agent(home, chosen)
    return chosen


def _remember_agent(home: Path, chosen: agents.Agent) -> None:
    """Persist the menu choice as ``agent.default`` — advisory: a config
    that cannot be written just means the menu asks again next time."""
    try:
        config = _readable_config(home)
        entry = config.get("agent")
        config["agent"] = {
            **(entry if isinstance(entry, dict) else {}),
            "default": chosen.name,
        }
        save_config(home, config)
        typer.echo(
            f"(remembered {chosen.name} as agent.default in "
            f"{tilde(home / 'config.json')})",
            err=True,
        )
    except OSError:
        pass


def _prompt_optional_path(label: str) -> Path | None:
    """An optional file path collected interactively: bare Enter skips,
    a path that does not exist re-prompts (a typo must not silently drop
    the user's baseline schema or golden set)."""
    while True:
        raw = typer.prompt(label, default="", err=True).strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if path.is_file():
            return path
        typer.echo(f"No such file: {raw} (Enter to skip)", err=True)


def run_build(
    ports: Ports,
    home: Path,
    jobs: store.JobStore,
    item_id: str,
    meta: dict,
    *,
    intent: str | None,
    existing_schema: Path | None,
    ground_truth: Path | None,
    workspace: Path | None,
    agent_name: str | None,
    as_json: bool,
) -> None:
    """The shared handoff body behind ``schema build`` and the post-parse
    offer: collect what ade can collect, launch the agent session seeded
    with the rest, then account for the deliverable."""
    source = meta.get("source") or item_id
    environment = meta.get("environment", DEFAULT_ENVIRONMENT)

    config = _readable_config(home)
    if agent_name is not None:
        # An explicit --agent bypasses the remembered default: it names
        # any known or config-declared agent, needing only a PATH hit.
        found = [
            agent
            for agent in (agents.custom_agent(config), *agents.KNOWN_AGENTS)
            if agent is not None
            and agent.name == agent_name
            and ports.which(agent.command[0])
        ]
    else:
        found = agents.detect(ports.which, config)
    if not found:
        message = agents.install_message()
        if agent_name is not None:
            message = f"agent {agent_name!r} is not available.\n{message}"
        exit_with(
            {"error": "no_agent", "agent": agent_name, "message": message},
            message,
            as_json=as_json,
            code=EXIT_FAILED,
        )
    agent = found[0] if len(found) == 1 else _choose_agent(
        ports, home, found, as_json=as_json
    )

    if intent is None:
        # --json required --intent before reaching here; this prompt is
        # interactive-path only.
        typer.echo(
            "Describe what to extract — fields, tables, anything the "
            "schema should capture (the skill refines it with you):",
            err=True,
        )
        intent = typer.prompt("Intent", err=True).strip()
    if not as_json:
        if existing_schema is None:
            existing_schema = _prompt_optional_path(
                "Existing schema to improve (path, Enter to skip)"
            )
        if ground_truth is None:
            ground_truth = _prompt_optional_path(
                "Golden ground truth (path, Enter to skip)"
            )

    if workspace is None:
        date = datetime.fromtimestamp(ports.clock.now()).strftime("%Y-%m-%d")
        workspace = home / "schema-runs" / f"{_slug(source)}-{date}"
    workspace.mkdir(parents=True, exist_ok=True)

    prompt = agents.seeded_prompt(
        agent=agent,
        intent=intent,
        job_item_id=item_id,
        source=source,
        store_dir=jobs.item_dir(item_id),
        workspace=workspace,
        environment=environment if environment != DEFAULT_ENVIRONMENT else None,
        existing_schema=existing_schema,
        ground_truth=ground_truth,
    )
    typer.echo(
        f"Launching {agent.name} in {tilde(workspace)} — the schema loop "
        "runs in that session; finish it to return here.",
        err=True,
    )
    try:
        exit_code = ports.run_agent([*agent.command, prompt], cwd=workspace)
    except OSError as error:
        exit_with(
            {"error": "agent_launch_failed", "agent": agent.name,
             "message": str(error)},
            f"Could not launch {agent.name}: {error}",
            as_json=as_json,
            code=EXIT_FAILED,
        )

    schema_path = workspace / agents.DELIVERABLE
    if not schema_path.is_file():
        exit_with(
            {
                "error": "no_deliverable",
                "agent": agent.name,
                "agent_exit_code": exit_code,
                "workspace": str(workspace),
                "message": "The agent session ended without "
                "deliverable/schema.json.",
            },
            f"The {agent.name} session ended (exit {exit_code}) without "
            f"producing {tilde(schema_path)}; the workspace is kept — "
            "re-run `ade schema build` to resume it.",
            as_json=as_json,
            code=EXIT_FAILED,
        )

    ref = items.short_id(jobs, item_id)
    extract_hint = f"ade extract {ref} --schema {schema_path}"
    emit(
        {
            "status": "schema_built",
            "job_item_id": item_id,
            "agent": agent.name,
            "workspace": str(workspace),
            "schema": str(schema_path),
            "extract_hint": extract_hint,
            # The skill's final iteration already ran this extraction, so
            # the run below is a free cache hit — but it is the caller's
            # to run, so the payload never claims it happened here.
            "extracted": False,
        },
        f"Schema built -> {tilde(schema_path)}"
        f"\n  workspace: {tilde(workspace)}  (eval set, iteration log, "
        "dispositions alongside)"
        f"\n  next:      {extract_hint}",
        as_json=as_json,
    )
    if as_json:
        return
    if typer.confirm(
        "Run that extraction now? (mostly a free cache hit)",
        default=True,
        err=True,
    ):
        code = ports.run_agent(
            reexec_argv("extract", item_id, "--schema", str(schema_path))
        )
        raise typer.Exit(code=code)


@schema_app.command()
def build(
    ctx: typer.Context,
    job_id_token: str = typer.Argument(
        ...,
        metavar="JOB_ITEM_ID",
        help="A completed parse job item id (or unambiguous prefix).",
    ),
    intent: str | None = typer.Option(
        None, "--intent",
        help="What to extract, in plain language (prompted interactively "
        "when omitted; required with --json).",
    ),
    existing_schema: Path | None = typer.Option(
        None, "--existing-schema", exists=True, dir_okay=False, readable=True,
        help="A schema to improve: the skill runs it unchanged first as "
        "the regression floor.",
    ),
    ground_truth: Path | None = typer.Option(
        None, "--ground-truth", exists=True, dir_okay=False, readable=True,
        help="Golden ground truth seeding the skill's eval set (JSON "
        "keyed by document filename, or flat CSV).",
    ),
    workspace: Path | None = typer.Option(
        None, "--workspace",
        help="Skill workspace directory (default: "
        "<ade-home>/schema-runs/<source>-<date>/; an existing one resumes).",
    ),
    agent_name: str | None = typer.Option(
        None, "--agent",
        help="Agent CLI to launch (claude, codex, or a config-declared "
        "name); default: the configured or only available one.",
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Build an extraction schema for a parsed document by handing it to
    an AI-agent session running the build-schema skill (measured
    iteration: real extractions, reviewer verdicts, converged-or-blocked
    stops). Ends by naming the delivered schema and offering the extract.
    """
    ports: Ports = ctx.obj
    home = ade_home()
    jobs = store.JobStore(home)
    # The agent session inherits this terminal — piped streams would feed
    # the interactive session garbage (and its TUI would corrupt a piped
    # stdout), so even --json requires a real terminal here.
    if not (
        ports.stdin_is_tty() and ports.stdout_is_tty() and ports.stderr_is_tty()
    ):
        message = (
            "schema build launches an interactive agent session and needs "
            "a real terminal on stdin/stdout/stderr."
        )
        exit_with(
            {"error": "no_terminal", "message": message},
            message,
            as_json=as_json,
            code=EXIT_USAGE,
        )
    if as_json and intent is None:
        message = "--json cannot prompt; pass --intent."
        exit_with(
            {"error": "intent_required", "message": message},
            message,
            as_json=as_json,
            code=EXIT_USAGE,
        )
    item_id, meta = _parse_item_or_exit(jobs, job_id_token, as_json=as_json)
    run_build(
        ports, home, jobs, item_id, meta,
        intent=intent,
        existing_schema=existing_schema,
        ground_truth=ground_truth,
        workspace=workspace,
        agent_name=agent_name,
        as_json=as_json,
    )


_OFFER_LABELS = (
    "1) Build an extraction schema with an AI agent",
    "2) Exit",
)


def offer_after_parse(
    ports: Ports, home: Path, jobs: store.JobStore, item_id: str
) -> None:
    """The post-parse doorway into ``run_build`` — only for a human at a
    real terminal. Callers pass control only outside ``--json``/
    ``--id-only``; the gates here keep the offer away from pipes, agent
    hosts, CI, and anyone who set ``schema_prompt: false``. Bare Enter
    exits (the summary's ``next:`` line already printed), so the offer
    never costs a keystroke of the old behavior."""
    if not (
        ports.stdin_is_tty() and ports.stdout_is_tty() and ports.stderr_is_tty()
    ):
        return
    surf = surface.detect(os.environ, stdout_is_tty=ports.stdout_is_tty())
    if surf.host is not None or surf.term == "ci":
        return
    config = _readable_config(home)
    if config.get("schema_prompt") is False:
        return
    try:
        index: int | None = None
        if os.environ.get("TERM") != "dumb":
            typer.echo(
                "\nBuild an extraction schema from this parse? (↑/↓ and Enter)",
                err=True,
            )
            try:
                index = term.select(
                    list(_OFFER_LABELS), default=1, getchar=ports.getchar
                )
            except term.Unsupported:
                pass  # the widget erased itself; the typed fallback re-lists
        else:
            typer.echo("\nBuild an extraction schema from this parse?", err=True)
        if index is None:
            for label in _OFFER_LABELS:
                typer.echo(f"  {label}", err=True)
            choice = typer.prompt("Choice", default="2", err=True).strip()
            index = 0 if choice == "1" else 1
        if index != 0:
            return
        # A missing agent CLI is non-fatal on this path: the parse
        # succeeded and the summary's next: line already serves — the
        # handoff just isn't installed yet.
        if not agents.detect(ports.which, config):
            typer.echo(agents.install_message(), err=True)
            return
        meta = jobs.read_json(item_id, "meta.json") or {}
        run_build(
            ports, home, jobs, item_id, meta,
            intent=None,
            existing_schema=None,
            ground_truth=None,
            workspace=None,
            agent_name=None,
            as_json=False,
        )
    except typer.Abort:
        # Esc/Ctrl-C declines the optional add-on; the parse already
        # succeeded and its exit code stays 0.
        typer.echo("", err=True)
        return
