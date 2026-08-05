"""``help`` — the whole-surface reference in one call (agent bootstrap).

One invocation teaches an unfamiliar agent the entire shipped surface
without N round trips: every command and flag, the output convention,
each verb's result shape, the exit states, and the store layout. The
command and flag inventory is generated from the same click tree that
executes, so that part can never drift from the shipped surface; the
curated layers (band grouping, result shapes, exit states, store layout,
topics) are drift-guarded by tests instead.

``help`` also carries the conceptual pages a per-command ``--help`` has
nowhere to put — ``ade help workflow`` (how the verbs compose),
``output``, ``credentials``, ``errors``. A cold reader can see the
pipeline before their first run instead of assembling it from ``next:``
hints (F6).
"""

from __future__ import annotations

from enum import Enum
from importlib.metadata import version as _installed_version

import typer
# typer vendors its click fork; these are the concrete node types the
# built command tree is made of (groups, leaf commands, and both
# parameter kinds), which is what `help` introspects.
from typer.core import TyperArgument, TyperGroup, TyperOption

from .extract import EXTRACT_ARTIFACTS
from .output import (
    EXIT_FAILED,
    EXIT_PENDING,
    EXIT_RATE_LIMITED,
    EXIT_USAGE,
    JSON_FLAG,
    emit,
    exit_with,
)
from .parse import PARSE_ARTIFACTS

# The four-band grouping from the proposal's command surface. A shipped
# command missing from every band still renders (under "unbanded") so
# coverage never silently narrows; the test suite asserts the bucket
# stays empty.
BANDS: list[tuple[str, list[str]]] = [
    (
        "credentials & CLI lifecycle",
        [
            "auth login",
            "auth status",
            "auth logout",
            "login",
            "logout",
            "version",
            "update",
            "help",
        ],
    ),
    ("network verbs — the ADE job contracts", ["parse", "extract"]),
    (
        "local read models",
        ["history list", "history clear", "find", "view", "crop"],
    ),
]

UNBANDED = "unbanded"

# Conceptual pages, reachable as commands (`ade help workflow`). Each is
# what no single command's --help can hold: how the verbs compose, what
# the output modes are, how auth resolves, what the failures mean. Kept
# terse — this is a reference card, not a manual.
TOPICS: list[dict] = [
    {
        "name": "workflow",
        "title": "How the verbs compose",
        "body": [
            "parse ─┬─> find ──> crop        look at an element as an image",
            "       ├─> view                 grounded page images + markdown",
            "       └─> extract ──> view     schema-shaped data, grounded",
            "",
            "1. parse    ensure a document is parsed. Prints a job item id;",
            "            every other verb takes it (or an unambiguous prefix).",
            "2. find     search that parse's elements locally — zero API calls.",
            "            Returns citation records: job_item_id, element_id,",
            "            type, page, box, text.",
            "3. crop     render element regions as PNGs: one --element-id, or a",
            "            batch by find's own filters (--type figure, --all).",
            "4. extract  ensure a schema extraction exists for a parse job item",
            "            (or bring-your-own markdown). It becomes its own job",
            "            item, referencing the parse — artifacts never copied.",
            "5. view     build the self-contained HTML viewer for a parse or an",
            "            extract item; --element-id emits a deep link to one",
            "            element — the citation contract.",
            "",
            "Every verb is a guarantee, not a request: re-running the same",
            "invocation is served from the local store for free, a pending run",
            "is resumed rather than resubmitted, and only --force re-bills.",
            "`history list` is the store's index when you have lost an id.",
        ],
    },
    {
        "name": "output",
        "title": "Output modes and where results live",
        "body": [
            "Default        human-readable text on stdout.",
            "--json         one stable JSON object/array — the whole result,",
            "               including the extraction itself. Never requires",
            "               reading a file out of the store.",
            "--id-only      just the id(s), one per line, for piping:",
            "                 JOB=$(ade parse -d f.pdf --id-only)",
            "                 ade find $JOB --type figure --id-only",
            "               Errors and hints go to stderr, so a captured id is",
            "               never a sentence. Takes precedence over --json.",
            "",
            "`ade help --json` carries every verb's result shape (the `result`",
            "block per command). Bulk parse artifacts stay on disk unless asked",
            "for: `ade parse -d f.pdf --include markdown --json`.",
            "",
            "Artifacts are also plain files under ~/.ade/jobs/<job-item-id>/ —",
            "a convenience, never the contract.",
        ],
    },
    {
        # Not "auth": `ade help auth` already scopes to the auth command
        # group, and a topic must never shadow a command path.
        "name": "credentials",
        "title": "Credentials, environments, and headless setup",
        "body": [
            "The target environment is resolved fresh on every command:",
            "--env, else $ADE_ENV, else production. Nothing is sticky, so a",
            "login and the verb after it can never disagree. Credentials are",
            "stored per environment in ~/.ade/credentials.json (mode 0600).",
            "",
            "Headless (no terminal to prompt on), in order of preference:",
            "  export ADE_API_KEY=...           overrides any stored credential",
            "  echo $KEY | ade auth login       a piped key is a prompt answered",
            "  ade auth login --api-key $KEY    inline (visible to `ps`)",
            "",
            "`ade auth status --json` reports the resolved target, how it is",
            "authenticated, and every other environment holding a credential.",
        ],
    },
    {
        "name": "errors",
        "title": "Exit states and error payloads",
        "body": [
            "Every non-success outcome is one payload shape: an `error` code",
            "plus a human `message` that names the remediation, on stdout with",
            "--json exactly like a result. Pending is not an error — it is a",
            "normal outcome with its own exit code and a resumable payload.",
            "",
            "Common codes: unknown_id / ambiguous_id (with candidates),",
            "not_parsed, unknown_element, no_parse_linkage, source_missing,",
            "no_credential, bad_source, bad_query.",
            "",
            "The recovery gesture for a pending run is always the same command,",
            "re-run: it joins the recorded job, never resubmits, never re-bills.",
        ],
    },
]

# Each verb's success payload, key by key — the published contract F7
# asked for, so a scripter reads the shape instead of discovering it. The
# `error` payload shape is universal and documented under the `errors`
# topic rather than repeated per command.
RESULTS: dict[str, dict] = {
    "parse": {
        "shape": "object",
        "keys": [
            ("status", "'parsed'"),
            ("run_id", "server-side run id (the wire's job_id)"),
            ("job_item_id", "store key every other verb takes"),
            ("environment", "resolved environment"),
            ("version", "resolved parse model version"),
            ("credits", "credits billed (0 on a cached hit)"),
            ("tier", "service tier the run was billed at"),
            ("page_count", "pages parsed"),
            ("failed_pages", "1-indexed pages the server could not parse"),
            ("cached", "true when served free from the store"),
            ("stored", "false when the result could not be persisted"),
            ("store_dir", "absolute path of the job item folder"),
            ("artifacts", "artifact filenames written there"),
            ("markdown", "the parse markdown — only with --include markdown"),
            ("elements", "the flat projection — only with --include elements"),
            ("kept_copy", "with --keep-copy: whether the URL document's "
             "copy was stored in the job item"),
            ("keep_copy_error", "with --keep-copy: why the copy could not "
             "be stored (the parse itself still succeeded)"),
        ],
    },
    "extract": {
        "shape": "object",
        "keys": [
            ("status", "'extracted'"),
            ("run_id", "server-side run id (the wire's job_id)"),
            ("job_item_id", "this extraction's own job item"),
            ("parse_job_item_id", "the parse it references (absent for markdown)"),
            ("environment", "resolved environment"),
            ("version", "resolved extract model version"),
            ("credits", "credits billed (0 on a cached hit)"),
            ("tier", "service tier the run was billed at"),
            ("extraction", "THE RESULT: the schema-shaped object, verbatim"),
            ("fields", "number of leaf fields"),
            ("ungroundable", "field paths whose non-empty value has no box"),
            ("empty_fields", "field paths with no value (nothing to ground)"),
            ("schema_violation_error", "set when the extraction is partial "
             "(strict=false skipped schema fields); null on a clean run"),
            ("warnings", "server warnings, verbatim ([] when none)"),
            ("evidence", "{kind, reason?, fields[]} — the field→box join"),
            ("cached", "true when served free from the store"),
            ("stored", "false when the result could not be persisted"),
            ("store_dir", "absolute path of the job item folder"),
            ("artifacts", "artifact filenames written there"),
            ("reused_parse", "the parse reused, when one was (no parse billed)"),
            ("parsed_first", "the standalone parse run first, when one was"),
        ],
    },
    "find": {
        "shape": "array",
        "keys": [
            ("job_item_id", "the parse item the match came from"),
            ("element_id", "element id — what crop and view take"),
            ("type", "element type (text, table, table_cell, figure, ...)"),
            ("page", "1-indexed page"),
            ("box", "{xmin, ymin, xmax, ymax} fractions of page size"),
            ("text", "the element's markdown slice"),
        ],
        "note": "One record per match, in document order; [] when nothing "
        "matched. --id-only prints the element ids alone.",
    },
    "crop": {
        "shape": "object",
        "keys": [
            ("status", "'cropped'"),
            ("job_item_id", "the addressed item (crops land under its crops/)"),
            ("count", "how many PNGs were written"),
            ("directory", "where they landed"),
            ("crops", "one record per PNG (element_id, type, "
             "page, box, dpi, path, width, height)"),
        ],
        "note": "One shape whatever matched: a single --element-id is "
        "count 1 with one crops[] record; a filter (--type/--page/--all) "
        "matching nothing is count 0 with crops [].",
    },
    "view": {
        "shape": "object",
        "keys": [
            ("status", "'viewed' ('cropped' with --crop, 'synced' with --sync-viewers)"),
            ("job_item_id", "the item rendered"),
            ("kind", "parse | extract"),
            ("path", "the self-contained view.html (or the PNG, with --crop)"),
            ("built", "true when this run rebuilt the artifact"),
            ("pages_embedded", "pages inlined; the rest load from sidecars"),
            ("note", "why the render weakened, when it did (else null)"),
            ("downloaded", "with --download: true when this run fetched "
             "the URL document into the job item (false: already attached)"),
            ("deep_link", "view.html#element=... when --element-id was given"),
            ("history_items", "items in the rebuilt sidebar read model"),
            ("sidebar_sync", "true when sibling viewers build in the background"),
        ],
    },
    "history list": {
        "shape": "array",
        "keys": [
            ("job_item_id", "the item"),
            ("kind", "parse | extract"),
            ("state", "derived from the ticket and artifacts on disk"),
            ("run_id", "server-side run id of the recorded generation"),
            ("source", "document path, URL, or markdown file"),
            ("params", "the invocation params, verbatim"),
            ("parse", "extract items: the referenced parse — {job_item_id, "
             "run_id (the parse generation extracted against), missing}"),
            ("stale", "extract items: true when the referenced parse was "
             "--force re-run after this extraction"),
            ("created_at / completed_at", "epoch seconds (null when unknown)"),
        ],
        "note": "Ordered newest submission first (timestamp-less items "
        "last), matching the viewer sidebar; --asc restores oldest-first. "
        "Capped at the newest 100 items by default — --limit N adjusts, "
        "--all lifts the cap, and a capped run says so on stderr. The "
        "--json array follows the same order and cap.",
    },
    "history clear": {
        "shape": "object",
        "keys": [
            ("cleared", "job item ids removed"),
            ("cascaded", "extract items removed with the parse they referenced"),
        ],
    },
    "auth login": {
        "shape": "object",
        "keys": [
            ("method", "api_key | oauth"),
            ("credential", "masked credential — never the secret"),
            ("stored", "true once written to credentials.json"),
            ("already_authenticated", "present when the target needed nothing"),
            ("environment", "the resolved target"),
            ("endpoint", "its endpoint"),
            ("endpoint_source", "default | config | env"),
            ("identity", "OAuth logins: the token's identity claims"),
        ],
    },
    "auth status": {
        "shape": "object",
        "keys": [
            ("authenticated", "false (exit 1) when the target has no credential"),
            ("method", "api_key | oauth"),
            ("credential", "masked credential"),
            ("source", "env (ADE_API_KEY) | stored"),
            ("environment / endpoint / endpoint_source", "the resolved target"),
            ("other_environments", "every other environment holding a credential"),
            ("expires_at / expires_in_seconds / refresh_token", "OAuth only"),
        ],
    },
    "auth logout": {
        "shape": "object",
        "keys": [
            ("logged_out", "always true — logout is idempotent"),
            ("cleared", "false when there was nothing stored to clear"),
            ("revoked", "refresh tokens revoked best-effort"),
            ("scope", "environment | all"),
            ("environment", "the environment cleared (null with --all)"),
        ],
    },
    "version": {
        "shape": "object",
        "keys": [
            ("version", "the installed ade version"),
            ("install", "how it is installed: binary (standalone app, "
             "self-updates via `ade update`) | python (uv/pipx — upgrade "
             "with `uv tool upgrade ade-cli`)"),
        ],
    },
    "update": {
        "shape": "object",
        "keys": [
            ("current", "the running version"),
            ("latest", "the latest released version (null when the release "
             "channel is not visible)"),
            ("updated", "true when this run installed the newer version"),
            ("install", "binary | python — python installs are never "
             "mutated, only pointed at `uv tool upgrade ade-cli`"),
        ],
    },
    "help": {
        "shape": "object",
        "keys": [
            ("cli / version / description", "what this binary is"),
            ("conventions", "the rules every command follows"),
            ("commands", "every command: usage, arguments, flags, result shape"),
            ("topics", "the conceptual pages (`ade help workflow`)"),
            ("exit_states", "every exit code and what it means"),
            ("store", "the on-disk layout"),
        ],
    },
}
# The top-level aliases are the same callbacks, so they are the same
# contract — recorded once, referenced twice.
RESULTS["login"] = RESULTS["auth login"]
RESULTS["logout"] = RESULTS["auth logout"]

# The machine-readable exit states every command shares (output.py; the
# values are imported, never restated, so this table cannot drift).
EXIT_STATES = [
    {
        "code": 0,
        "name": "ok",
        "meaning": "Success — the command's payload is on stdout.",
    },
    {
        "code": EXIT_FAILED,
        "name": "failed",
        "meaning": "The run failed or the target cannot serve the request "
        "(job failure, unknown id, missing parse, unreadable result).",
    },
    {
        "code": EXIT_USAGE,
        "name": "usage",
        "meaning": "The invocation itself was wrong; nothing was submitted.",
    },
    {
        "code": EXIT_PENDING,
        "name": "pending",
        "meaning": "The wait budget expired while the run continues "
        "server-side — a normal outcome, not an error. The payload carries "
        "a 'pending' status plus the run_id and job_item_id; re-run the same "
        "command to resume polling (never resubmits, never re-bills).",
    },
    {
        "code": EXIT_RATE_LIMITED,
        "name": "rate_limited",
        "meaning": "Submit was rate-limited and the wait budget ran out "
        "before a job existed; nothing was submitted, nothing bills. "
        "Re-run to retry.",
    },
]

CONVENTIONS = [
    (
        "--json",
        "Every command supports --json: one stable JSON object/array on "
        "stdout (errors and pending payloads follow the same rule). Agents "
        "should always pass it. Each command's published shape is its "
        "'result' block below — the full result is always on stdout, never "
        "only in a file.",
    ),
    (
        "--id-only",
        "parse, extract, and find also take --id-only: just the id(s), one "
        "per line, for piping (JOB=$(ade parse -d f.pdf --id-only)). Errors "
        "and hints go to stderr so a captured id is never a sentence.",
    ),
    (
        "job item ids",
        "Store commands take a job item id or an unambiguous prefix. "
        "Discover ids with `history list`; ambiguous or unknown ids error "
        "with candidates listed. Distinct from the server-side run id: "
        "--json payloads report that as run_id, and on-disk records spell "
        "the same value job_id (the wire's name) — neither is ever a "
        "job item id.",
    ),
    (
        "guarantees",
        "parse and extract ensure a run exists rather than fire a request: "
        "an already-done run is served from disk free with an explicit "
        "notice (--force consents to a re-bill); a pending run is resumed, "
        "never resubmitted; Ctrl-C stops the waiting, not the work.",
    ),
    (
        "env overrides",
        "ADE_HOME relocates the store; ADE_API_KEY overrides stored "
        "credentials; ADE_ENDPOINT overrides the stored endpoint.",
    ),
]

# Store layout, flat by design: one top-level folder per run under jobs/.
# Artifact names come from the modules that write them.
STORE_LAYOUT = [
    {
        "path": "~/.ade/config.json",
        "what": "endpoint + named-environment config",
    },
    {
        "path": "~/.ade/credentials.json",
        "what": "per-environment credentials (mode 0600; written by "
        "`auth login`)",
    },
    {
        "path": "~/.ade/history.js",
        "what": "sidebar read model over jobs/, rewritten from a fresh "
        "store scan by every view/history run",
    },
    {
        "path": "~/.ade/jobs/<job-item-id>/",
        "what": "one folder per run — parse and extract alike, all "
        "top-level siblings (flat, never nested)",
    },
    {
        "path": "  meta.json",
        "what": "commit record: kind, source, identity, params, state, "
        "timestamps, artifact index. Its job_id field is the server-side "
        "run id (= run_id in --json payloads), never the job item id",
    },
    {
        "path": "  job.json",
        "what": "claim ticket: the server-side run id (spelled job_id on "
        "disk), tier, state — what a re-run resumes",
    },
    {
        "path": "  " + " / ".join(PARSE_ARTIFACTS),
        "what": "parse items: the raw ParseResponse verbatim, its markdown, "
        "and the flat elements projection (grounding inline)",
    },
    {
        "path": "  " + " / ".join(EXTRACT_ARTIFACTS),
        "what": "extract items: the raw result verbatim and the local "
        "field→box evidence join",
    },
    {
        "path": "  parse/ref.json",
        "what": "parse-backed extract items: the reference to the parse "
        "job item (artifacts are never copied)",
    },
    {
        "path": "  markdown.md",
        "what": "bring-your-own-markdown extract items: the input markdown, "
        "copied in (spans index exactly these bytes)",
    },
    {
        "path": "  document.<ext>",
        "what": "URL parses: the attached document copy (`parse "
        "--keep-copy` / `view --download`) page previews and crops render "
        "from — unverified against the parsed run",
    },
    {
        "path": "  view.html / crops/",
        "what": "derived artifacts: the self-contained viewer and PNG "
        "crops — recomputable, built on demand",
    },
]


def _walk(group: TyperGroup, prefix: tuple[str, ...] = ()) -> list[tuple[str, object]]:
    """Every runnable, non-hidden command in the tree as ('auth login',
    command) pairs, in registration order."""
    found: list[tuple[str, object]] = []
    for name, command in group.commands.items():
        if command.hidden:
            continue
        if isinstance(command, TyperGroup):
            found.extend(_walk(command, (*prefix, name)))
        else:
            found.append((" ".join((*prefix, name)), command))
    return found


def _metavar(param) -> str | None:
    if isinstance(param, TyperOption) and param.is_flag:
        return None
    if param.metavar:
        return param.metavar
    choices = getattr(param.type, "choices", None)
    if choices:
        return "|".join(str(choice) for choice in choices)
    # e.g. "integer range" → INTEGER; the range detail lives in the help text
    return (param.type.name or "value").upper().split()[0]


def _default(param) -> object:
    value = param.default
    if isinstance(value, Enum):
        value = value.value
    if value in (None, False) or value == [] or callable(value):
        return None
    return value


def _command_record(name: str, command) -> dict:
    arguments: list[dict] = []
    flags: list[dict] = []
    supports_json = False
    for param in command.params:
        if param.name == "help":
            continue
        if isinstance(param, TyperArgument):
            arguments.append(
                {
                    "name": param.metavar or param.name.upper(),
                    "required": param.required,
                    "help": getattr(param, "help", None),
                }
            )
            continue
        assert isinstance(param, TyperOption)
        if param.hidden:
            continue
        if param.name == "as_json":
            # Documented once, under conventions — every command carries it
            # (test-guarded), so repeating it per command is noise.
            supports_json = True
            continue
        flags.append(
            {
                # Paired booleans (--open/--no-open) keep the off-switch in
                # secondary_opts; the reference must show both spellings.
                "flags": ", ".join([*param.opts, *param.secondary_opts]),
                "metavar": _metavar(param),
                "required": param.required,
                "default": _default(param),
                "help": param.help,
            }
        )
    result = RESULTS.get(name)
    return {
        "name": name,
        "usage": _usage(name, arguments, flags),
        "summary": command.help or "",
        "arguments": arguments,
        "flags": flags,
        "supports_json": supports_json,
        # The published --json shape (F7). Curated, so a command shipped
        # without one renders as null and fails the drift test.
        "result": (
            {
                "shape": result["shape"],
                "keys": [{"key": key, "what": what} for key, what in result["keys"]],
                **({"note": result["note"]} if "note" in result else {}),
            }
            if result is not None
            else None
        ),
    }


def _usage(name: str, arguments: list[dict], flags: list[dict]) -> str:
    pieces = [f"ade {name}"]
    for argument in arguments:
        spelled = argument["name"]
        if argument["required"] or spelled.startswith("["):
            pieces.append(spelled)
        else:
            pieces.append(f"[{spelled}]")
    for flag in flags:
        if flag["required"]:
            long_opt = flag["flags"].split(", ")[-1]
            pieces.append(f"{long_opt} {flag['metavar'] or ''}".rstrip())
    if any(not flag["required"] for flag in flags):
        pieces.append("[options]")
    pieces.append("[--json]")
    return " ".join(pieces)


def _banded(records: list[dict]) -> list[dict]:
    """Records annotated with their band, in band order; anything shipped
    but unbanded still renders (and fails the drift test)."""
    by_name = {record["name"]: record for record in records}
    ordered: list[dict] = []
    for band, names in BANDS:
        for name in names:
            if name in by_name:
                ordered.append({**by_name.pop(name), "band": band})
    ordered.extend({**record, "band": UNBANDED} for record in by_name.values())
    return ordered


def _human(reference: dict, *, scoped: bool) -> str:
    lines: list[str] = []
    if not scoped:
        lines.append(
            f"ade {reference['version']} — {reference['description']}"
        )
        lines.append("")
        lines.append("conventions")
        for convention in reference["conventions"]:
            lines.append(f"  {convention['name']}")
            lines.append(f"      {convention['rule']}")
        lines.append("")
    band = None
    for record in reference["commands"]:
        if not scoped and record["band"] != band:
            band = record["band"]
            lines.append(band)
        lines.append(f"  {record['usage']}")
        for text_line in record["summary"].splitlines():
            lines.append(f"      {text_line.strip()}")
        for argument in record["arguments"]:
            spelled = argument["name"] + ("" if argument["required"] else " (optional)")
            lines.append(f"        {spelled:<24}  {argument['help'] or ''}".rstrip())
        for flag in record["flags"]:
            spelled = flag["flags"] + (f" {flag['metavar']}" if flag["metavar"] else "")
            lines.append(f"        {spelled}")
            if flag["help"]:
                lines.append(f"            {flag['help']}")
            if flag["default"] is not None:
                lines.append(f"            (default: {flag['default']})")
        # The result shape only when the reader asked about this one
        # command; in the whole-surface listing it would bury the
        # inventory (it is always in --json, which is what agents read).
        if scoped and record["result"]:
            lines.append(f"      result ({record['result']['shape']})")
            for entry in record["result"]["keys"]:
                lines.append(f"        {entry['key']:<24}  {entry['what']}")
            if record["result"].get("note"):
                lines.append(f"        note: {record['result']['note']}")
        lines.append("")
    if scoped:
        return "\n".join(lines).rstrip()
    lines.append("exit states")
    for state in reference["exit_states"]:
        lines.append(f"  {state['code']}  {state['name']:<13} {state['meaning']}")
    lines.append("")
    lines.append("store layout")
    for entry in reference["store"]["layout"]:
        lines.append(f"  {entry['path']:<42}  {entry['what']}")
    lines.append(f"  note: {reference['store']['note']}")
    lines.append("")
    lines.append("topics — conceptual pages, e.g. `ade help workflow`")
    for topic in reference["topics"]:
        lines.append(f"  {topic['name']:<12}  {topic['title']}")
    return "\n".join(lines)


def _topic_human(topic: dict) -> str:
    return "\n".join([f"{topic['name']} — {topic['title']}", "", *topic["body"]])


def help_command(
    ctx: typer.Context,
    tokens: list[str] | None = typer.Argument(
        None,
        metavar="[COMMAND|TOPIC]...",
        help="Scope the reference to one command or group (`help extract`, "
        "`help auth login`), or print a topic: "
        + ", ".join(topic["name"] for topic in TOPICS)
        + ".",
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Print the whole-surface command reference in one call: every
    command and flag, the output convention, each verb's result shape,
    exit states, and the store layout. The agent bootstrap — run this
    (with --json) before anything else. `help TOPIC` prints one
    conceptual page instead (workflow, output, credentials, errors)."""
    root = ctx.find_root().command
    assert isinstance(root, TyperGroup)
    records = _banded([_command_record(name, cmd) for name, cmd in _walk(root)])

    scope = " ".join(tokens or [])
    if scope:
        # Topics resolve first: they are their own page, not a slice of the
        # command inventory. No topic shares a name with a command (the
        # drift test holds that), so the precedence hides nothing.
        for topic in TOPICS:
            if topic["name"] == scope:
                emit(topic, _topic_human(topic), as_json=as_json)
                return
        scoped = [
            record
            for record in records
            if record["name"] == scope or record["name"].startswith(scope + " ")
        ]
        if not scoped:
            exit_with(
                {
                    "error": "unknown_command",
                    "command": scope,
                    "candidates": [record["name"] for record in records],
                    "topics": [topic["name"] for topic in TOPICS],
                },
                f"Unknown command or topic {scope!r}. Commands: "
                + ", ".join(record["name"] for record in records)
                + ". Topics: "
                + ", ".join(topic["name"] for topic in TOPICS)
                + ".",
                as_json=as_json,
                code=EXIT_USAGE,
            )
        records = scoped

    reference = {
        "cli": "ade",
        "version": _installed_version("ade-cli"),
        "description": root.help or "",
        "conventions": [
            {"name": name, "rule": rule} for name, rule in CONVENTIONS
        ],
        "commands": records,
        "topics": TOPICS,
        "exit_states": EXIT_STATES,
        "store": {
            "home": "~/.ade (ADE_HOME overrides)",
            # One vocabulary note for every on-disk record: job_id there
            # is the wire's spelling of the run id.
            "note": "On-disk records (meta.json, job.json, parse/ref.json) "
            "spell the server-side run id as job_id — the wire contract's "
            "name for the same value --json payloads report as run_id. "
            "Neither is the job item id (the jobs/<id>/ folder name).",
            "layout": STORE_LAYOUT,
        },
    }
    emit(reference, _human(reference, scoped=bool(scope)), as_json=as_json)
