"""``history`` — the read model over the job-item store: list and clear —
plus the id-resolution error plumbing other commands share
(``resolve_or_exit``).

Every command here costs zero API calls; states derive from tickets and
artifacts on disk. Every run re-scans ``jobs/`` and rewrites ``history.js``
from that same scan, so listings and the sidebar read model heal together
after manual deletion.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime

import typer

from . import historyjs, items, store
from .config import ade_home
from .output import EXIT_FAILED, EXIT_USAGE, JSON_FLAG, emit, tilde
from .ports import Ports

history_app = typer.Typer(name="history", help="Inspect and manage the local job-item store.")


@history_app.callback(invoke_without_command=True)
def _history_default(ctx: typer.Context, as_json: bool = JSON_FLAG) -> None:
    """Inspect and manage the local job-item store; bare `ade history`
    defaults to `ade history list`."""
    if ctx.invoked_subcommand is None:
        list_items(ctx, as_json=as_json)


def require_job_id(token: str | None, *, as_json: bool) -> str:
    """Usage gate for commands targeting one JOB_ID argument. Typer's own
    missing-argument error bypasses the output convention (no JSON on
    stdout, no remediation), so the argument is optional at the CLI layer
    and required here, where the error can follow the contract."""
    if token is not None:
        return token
    message = (
        "Provide a job item id (or unambiguous prefix); run "
        "`ade history list` to see the store."
    )
    # "job_item_id", never "job_id": the latter names the *server* job in
    # every machine payload (CONTEXT.md keeps the two distinct).
    emit({"error": "missing_job_item_id", "message": message}, message, as_json=as_json)
    raise typer.Exit(code=EXIT_USAGE)


def resolve_or_exit(jobs: store.JobStore, token: str, *, as_json: bool) -> str:
    try:
        return items.resolve(jobs, token)
    except items.IdError as error:
        payload = {"error": error.kind, "id": error.token, "message": error.message}
        human = error.message
        if error.candidates:
            payload["candidates"] = error.candidates
            human += "\nCandidates:\n" + "\n".join(f"  {c}" for c in error.candidates)
        emit(payload, human, as_json=as_json)
        code = EXIT_USAGE if error.kind == "ambiguous_id" else EXIT_FAILED
        raise typer.Exit(code=code)


def _grouped(records: list[dict]) -> list[tuple[dict, bool]]:
    """History order with linkage: every parse item (and every extract item
    without a living parent) is a top row; extract items referencing a
    still-present parse render indented beneath it."""
    children: dict[str, list[dict]] = {}
    tops: list[dict] = []
    for record in records:
        ref = record.get("parse") or {}
        if ref.get("job_item_id") and not ref.get("missing"):
            children.setdefault(ref["job_item_id"], []).append(record)
        else:
            tops.append(record)
    rows: list[tuple[dict, bool]] = []
    for top in tops:
        rows.append((top, False))
        rows.extend((child, True) for child in children.get(top["job_item_id"], []))
    return rows


# The STATE column is the one people scan; color it by value.
_STATE_STYLES = {
    "parsed": "green",
    "extracted": "green",
    "pending": "yellow",
    "failed": "red",
    "unreadable": "red",
}


@history_app.command("list")
def list_items(ctx: typer.Context, as_json: bool = JSON_FLAG) -> None:
    """List stored job items: id, kind, state, env, params, source. Extract
    items referencing a parse item indent beneath it. Bare `ade history`
    defaults to this command."""
    jobs = store.JobStore(ade_home())
    ports: Ports = ctx.obj
    records = items.item_records(jobs)
    historyjs.write(jobs, records, now=ports.clock.now())
    rows = _grouped(records)
    if not as_json and ports.stdout_is_tty() and records:
        # A real table is a TTY-only upgrade; piped output below stays
        # line-oriented (one row per item, children indented).
        _render_table(jobs, rows)
        return
    human = "\n".join(_plain_line(record, indent) for record, indent in rows)
    emit(records, human or "No job items stored.", as_json=as_json)


def _plain_line(record: dict, indent: bool) -> str:
    line = (
        ("  " if indent else "")
        + f"{record['job_item_id']}  {record['kind']:<7}  "
        # Widths fit the longest state ("unreadable") and environment
        # ("production") so columns stay aligned; items from before the
        # environment field read as "?", never a guessed default.
        + f"{record['state']:<10}  {record.get('environment') or '?':<10}  "
        # When the run was submitted — the ordering key, so the listing's
        # oldest-first order is legible. Local time, like the table.
        + f"{_submitted_cell(record):<16}  "
        + f"{items.compact_params(record)}  "
        + (record["source"] or "?")
    )
    if (record.get("parse") or {}).get("missing"):
        line += "  (parse missing)"
    if record["reason"]:
        # An unreadable ticket's diagnosis, visible without --json.
        line += f"\n    reason: {record['reason']}"
    if record.get("schema_violation_error"):
        # A partial extraction (#118), visible without --json.
        line += (
            "\n    partial: "
            + record["schema_violation_error"].splitlines()[0]
        )
    if record.get("warnings"):
        count = record["warnings"]
        line += f"\n    warnings: {count} server warning(s) in extract.json"
    return line


def _render_table(jobs: store.JobStore, rows: list[tuple[dict, bool]]) -> None:
    # rich rides in with typer (>=0.12 depends on it); imported lazily so
    # the plumbing-friendly paths never pay for it.
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    try:
        # Explicit COLUMNS wins; otherwise rich measures the terminal. Read
        # here rather than trusting rich's fallback chain, which can reach
        # through to the parent terminal even when stdout is redirected.
        width = int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        width = None
    console = Console(width=width)

    cells = [
        [
            # Child rows (extracts under their parse) carry a tree marker,
            # not bare indentation — two leading spaces read as misalignment.
            ("└ " if indent else "") + record["job_item_id"],
            record["kind"],
            record["state"],
            # Items from before the environment field read as "?", never a
            # guessed default.
            record.get("environment") or "?",
            _submitted_cell(record),
            items.compact_params(record),
        ]
        for record, indent in rows
    ]
    # "(local)" rather than today's zone abbreviation: each row converts
    # its own epoch with the zone rules in effect *then*, so one history
    # can legitimately span DST names — a current-time label would
    # mislabel half of it.
    headers = ["JOB ITEM", "KIND", "STATE", "ENV", "SUBMITTED (local)", "PARAMS"]
    widths = [
        max(len(header), *(len(row[i]) for row in cells))
        for i, header in enumerate(headers)
    ]
    # What a truncated params cell must keep visible: the model and the
    # tier — only the middle (a pages list) may elide.
    elided = [items.elided_params(record) for record, _ in rows]
    keep = max(len("PARAMS"), *(len(text) for text in elided))
    # SUBMITTED is the first column to yield: on a terminal too narrow to
    # hold it alongside the un-elidable params and the SOURCE floor it
    # drops entirely — a partial timestamp is noise, and the exact epoch
    # lives in --json.
    if console.width < sum(widths[:5]) + min(keep, 40) + 8 + _overhead(len(headers)):
        headers.pop(4)
        widths.pop(4)
        cells = [row[:4] + row[5:] for row in cells]
    # The identity columns (id, kind, state, env, submitted) are naturally
    # narrow and fix at content width so rich never shrinks them. PARAMS
    # takes what the terminal leaves after those plus a floor for SOURCE —
    # so no column ever pushes another off-screen.
    last = len(headers) - 1  # PARAMS
    overhead = _overhead(len(headers))
    widths[last] = min(
        widths[last],
        40,  # a params cell is a summary; past this, SOURCE needs it more
        max(12, console.width - sum(widths[:last]) - overhead - 12),
    )
    # A cell that overflows the column swaps to its elided form (model and
    # tier intact) — even when the elided form itself must fold, folding
    # the bounded model/…/tier beats folding a long pages list.
    for row, short in zip(cells, elided):
        if len(row[last]) > widths[last]:
            row[last] = short
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, header_style="bold")
    for header, width_ in zip(headers[:last], widths[:last]):
        # width alone is advisory under overflow; min_width pins it so a
        # narrow terminal crops SOURCE, never the identity columns.
        table.add_column(header, no_wrap=True, width=width_, min_width=width_)
    # overflow="fold" wraps params onto continuation lines within the cell
    # (even a single long token); the full value lives in --json.
    table.add_column(
        "PARAMS", width=widths[last], min_width=widths[last], overflow="fold"
    )
    table.add_column("SOURCE", no_wrap=True)

    # Budget for SOURCE: exactly what the other columns and the overhead
    # leave over, so the table never exceeds the terminal. Sources truncate
    # from the left so the basename stays visible; the full source lives
    # in --json.
    budget = max(8, console.width - sum(widths) - overhead)
    # Annotation rows put their text in the SOURCE column, blank elsewhere.
    blanks = [""] * len(headers)
    for (record, _indent), row in zip(rows, cells):
        source = tilde(record["source"]) if record["source"] else "?"
        if (record.get("parse") or {}).get("missing"):
            source += "  (parse missing)"
        if len(source) > budget:
            source = "…" + source[-(budget - 1):]
        table.add_row(
            row[0],
            row[1],
            Text(row[2], style=_STATE_STYLES.get(row[2], "")),
            *row[3:],
            source,
        )
        if record["reason"]:
            # An unreadable ticket's diagnosis, visible without --json;
            # cropped to the column (the full text lives in --json).
            reason = f"reason: {record['reason']}"
            if len(reason) > budget:
                reason = reason[: budget - 1] + "…"
            table.add_row(*blanks, Text(reason, style="dim"))
        if record.get("schema_violation_error"):
            # A partial extraction (#118): advisory, like the playground's
            # amber toast — yellow, never the failure red.
            partial = (
                "partial: "
                + record["schema_violation_error"].splitlines()[0]
            )
            if len(partial) > budget:
                partial = partial[: budget - 1] + "…"
            table.add_row(*blanks, Text(partial, style="yellow"))
        if record.get("warnings"):
            warn = (
                f"warnings: {record['warnings']} server warning(s) "
                "in extract.json"
            )
            if len(warn) > budget:
                warn = warn[: budget - 1] + "…"
            table.add_row(*blanks, Text(warn, style="yellow"))
    console.print(table)


def _submitted_cell(record: dict) -> str:
    """The compact submission time, in this machine's local zone with the
    rules in effect at that epoch (DST included) — listings are read
    where the runs happened. Items predating the field read as "?"; the
    raw epoch stays in --json (``submitted_at``)."""
    epoch = record.get("submitted_at")
    if epoch is None:
        return "?"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _overhead(columns: int) -> int:
    """rich's per-column cost under box.SIMPLE with pad_edge=False, for
    ``columns`` data columns plus SOURCE: two padding cells per column
    minus the outer pair, plus one divider cell between columns.
    Undercounting this is what used to tip rich into its overflow crop
    cascade, which shaves even min_width-pinned columns."""
    total = columns + 1  # + SOURCE
    return (2 * total - 2) + (total - 1)


@history_app.command("clear")
def clear(
    ctx: typer.Context,
    job_id: str | None = typer.Argument(
        None, metavar="[JOB_ID]", help="Job item id or unambiguous prefix."
    ),
    clear_all: bool = typer.Option(False, "--all", help="Delete every stored job item."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Delete stored job items. Clearing a parse item cascades — with
    notice — to the extract items referencing it, so the store never holds
    dangling refs."""
    if (job_id is None) == (not clear_all):
        emit(
            {"error": "bad_target", "message": "Provide exactly one of JOB_ID or --all."},
            "Provide exactly one of JOB_ID or --all.",
            as_json=as_json,
        )
        raise typer.Exit(code=EXIT_USAGE)
    jobs = store.JobStore(ade_home())
    ports: Ports = ctx.obj
    cascaded: list[str] = []
    # The store lock makes the dependent scan and the deletions it drives
    # one atomic sweep against a concurrent clear. (Item creation doesn't
    # take it; residue from that race degrades to the explicit
    # parse-missing state on the next scan, like any manual deletion.)
    with jobs.store_lock():
        if clear_all:
            # Everything under <home>/jobs goes — items, crash residue, and
            # stray files alike; config and credentials are identity, not
            # store, and live outside it. Only real items are reported as
            # cleared.
            cleared = items.item_ids(jobs)
            root = jobs.jobs_root
            if root.is_dir():
                for entry in root.iterdir():
                    if entry.name == ".store.lock":
                        continue  # the mutex we're holding, not store content
                    # A symlink is a stray entry, not an item dir: recursing
                    # would lock and delete inside its target, outside the
                    # store.
                    if entry.is_dir() and not entry.is_symlink():
                        _remove_item_dir(jobs, entry.name)
                    else:
                        entry.unlink()
            human = "\n".join(f"Cleared {item_id}" for item_id in cleared) or "Store already empty."
        else:
            assert job_id is not None  # guaranteed by the target check above
            target = resolve_or_exit(jobs, job_id, as_json=as_json)
            cascaded = items.referencing_extracts(jobs, target)
            cleared = [target, *cascaded]
            # Dependents first: if the sweep dies mid-way, what survives is
            # a parse item missing some extracts — never a dangling ref.
            for item_id in [*cascaded, target]:
                _remove_item_dir(jobs, item_id)
            human = f"Cleared {target}"
            if cascaded:
                plural = "s" if len(cascaded) != 1 else ""
                human += (
                    f" + {len(cascaded)} dependent extract{plural} "
                    f"({', '.join(cascaded)})"
                )
    # The sidebar read model heals from the same operation that mutated the
    # store — a cleared item disappears without waiting for the next list.
    historyjs.refresh(jobs, now=ports.clock.now())
    emit({"cleared": cleared, "cascaded": cascaded}, human, as_json=as_json)


def _remove_item_dir(jobs: store.JobStore, item_id: str) -> None:
    # Deletion is a store mutation like any other: take the item lock so it
    # serializes with ticket transitions and artifact publication instead of
    # racing a mid-write mutator. A guarantee still polling after this
    # survives the sweep — its publication gate (ticket-owns-the-slot) reads
    # the emptied slot and declines to re-persist.
    with jobs.lock(item_id):
        shutil.rmtree(jobs.item_dir(item_id))
