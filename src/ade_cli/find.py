"""``find`` — local element search, the id-discovery loop-closer.

Pure filtering over a parse job item's elements projection: zero API
calls, all filters compose (AND), nothing is ever ranked. Output records
are the citation currency ``{job_item_id, element_id, type, page, box,
text}`` in document order (page, then reading order), items in the order
their ids were given — directly consumable by deep links and answer
citations.
"""

from __future__ import annotations

import re
from typing import NoReturn

import typer

from . import elements, items, store
from .config import ade_home
from .history import resolve_or_exit
from .output import (
    EXIT_FAILED,
    EXIT_USAGE,
    ID_ONLY_FLAG,
    JSON_FLAG,
    emit,
    set_id_only,
)


def find(
    tokens: list[str] | None = typer.Argument(
        None,
        metavar="[JOB_ITEM_ID] [QUERY]",
        help="Job item id (or unambiguous prefix) to search, then an "
        "optional case-insensitive substring QUERY. With --job, the one "
        "allowed positional is the QUERY.",
    ),
    job_tokens: list[str] = typer.Option(
        [],
        "--job",
        help="Parse job item to search (id or unambiguous prefix); "
        "repeatable for multi-item search. Equivalent to the positional "
        "JOB_ITEM_ID for a single item.",
    ),
    regex: bool = typer.Option(
        False,
        "--regex",
        help="Treat QUERY as a regular expression (case-insensitive; scope with (?-i:...)).",
    ),
    element_type: str | None = typer.Option(
        None, "--type", help="Element type (text, table, table_cell, figure, ...)."
    ),
    page: int | None = typer.Option(None, "--page", help="1-indexed page number."),
    element_ids: list[str] = typer.Option(
        [], "--element-id", help="Exact element id to match; repeatable."
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Return at most this many matches."
    ),
    as_json: bool = JSON_FLAG,
    id_only: bool = ID_ONLY_FLAG,
) -> None:
    """Search parsed elements locally: `find JOB_ITEM_ID [QUERY]`, or --job
    (repeatable) for several items; no query lists every element.

    Ids discovered here are what `view --element-id` deep-links and
    `crop --element-id` renders — though `crop` takes these same filters
    directly (`crop JOB_ITEM_ID --type figure`) when you want the images
    rather than the records.
    """
    set_id_only(id_only)

    def exit_usage(message: str) -> NoReturn:
        emit({"error": "bad_query", "message": message}, message, as_json=as_json)
        raise typer.Exit(code=EXIT_USAGE)

    # One calling convention across the surface: the job id is positional
    # here like everywhere else; --job stays for the multi-item case. With
    # --job given the positional slot holds only the QUERY.
    positionals = list(tokens or [])
    if job_tokens:
        if len(positionals) > 1:
            exit_usage(
                "Too many arguments: with --job, pass at most one QUERY."
            )
        query = positionals[0] if positionals else None
    else:
        if not positionals:
            exit_usage(
                "Provide a job item id (or unambiguous prefix): "
                "`ade find JOB_ITEM_ID [QUERY]`, or --job JOB_ITEM_ID (repeatable); "
                "run `ade history list` to see the store."
            )
        if len(positionals) > 2:
            exit_usage("Too many arguments: `ade find JOB_ITEM_ID [QUERY]`.")
        job_tokens = positionals[:1]
        query = positionals[1] if len(positionals) == 2 else None
    pattern: re.Pattern[str] | None = None
    if regex:
        if query is None:
            exit_usage("--regex needs a QUERY to compile.")
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as error:
            exit_usage(f"Invalid regex {query!r}: {error}.")

    jobs = store.JobStore(ade_home())
    item_ids: list[str] = []
    for token in job_tokens:
        item_id = resolve_or_exit(jobs, token, as_json=as_json)
        if item_id not in item_ids:  # the same item twice is one item, not two result sets
            item_ids.append(item_id)

    matches: list[dict] = []
    for item_id in item_ids:
        records = elements.live_elements(jobs, item_id)
        if records is None:
            record = items.item_record(jobs, item_id)
            if record["kind"] != "parse":
                # find searches parse items only; an extract item's elements
                # live in the parse it references — name it instead of
                # dead-ending at a parse command that would never help.
                ref = record.get("parse") or {}
                parse_item_id = ref.get("job_item_id")
                if parse_item_id and not ref.get("missing"):
                    human = (
                        f"Job item {item_id} is an extract item; `find` "
                        f"searches parse items — try `--job {parse_item_id}`."
                    )
                elif parse_item_id:
                    # The referenced parse was deleted: name it (same
                    # diagnosis crop's no_parse_linkage gives) instead of
                    # pretending the extract never had one — but never
                    # suggest `find --job` against a missing item.
                    human = (
                        f"Job item {item_id} references parse job item "
                        f"{parse_item_id}, which was deleted; nothing to "
                        "search."
                    )
                else:
                    human = (
                        f"Job item {item_id} is an extract item with no "
                        "parse to search."
                    )
                # message rides in the payload too — the machine-output
                # convention extract.py's not_a_parse_item already set.
                payload = {
                    "error": "not_a_parse_item",
                    "job_item_id": item_id,
                    "message": human,
                }
                if parse_item_id:
                    payload["parse_item_id"] = parse_item_id
                emit(payload, human, as_json=as_json)
                raise typer.Exit(code=EXIT_FAILED)
            state = record["state"]
            hint = (
                "a parse is pending; re-run `ade parse` to finish it"
                if state == "pending"
                else "run `ade parse` first"
            )
            emit(
                {"error": "not_parsed", "job_item_id": item_id, "state": state},
                f"Job item {item_id} has no completed parse ({hint}).",
                as_json=as_json,
            )
            raise typer.Exit(code=EXIT_FAILED)
        matches.extend(
            {
                "job_item_id": item_id,
                "element_id": record["id"],
                "type": record["type"],
                "page": record["page"],
                "box": record["box"],
                "text": record["text"],
            }
            for record in elements.select(
                records,
                element_type=element_type,
                page=page,
                element_ids=element_ids,
                query=query,
                pattern=pattern,
            )
        )
    if limit is not None:
        matches = matches[:limit]

    human = "\n".join(
        f"{m['job_item_id']}  {m['element_id']:<14}  {m['type']:<10}  p{m['page']}  "
        + _one_line(m["text"])
        for m in matches
    )
    emit(matches, human or "No matches.", as_json=as_json)


def _one_line(text: str, width: int = 72) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"
