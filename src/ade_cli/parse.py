"""``parse`` — the guarantee: ensure this exact run exists.

Keyed by the job item id (verb × source path × content × params — see
store.py): an exact match is served from disk free; any component
differing mints a sibling job item, so variants coexist and nothing is
silently replaced. Resumable (a pending claim ticket is joined, never
resubmitted) and interrupt-safe (Ctrl-C stops the waiting, not the work)
— the lifecycle itself lives in ``guarantee.py``. Artifacts on
completion: the raw ParseResponse (``parse.json``), the markdown with its
doc_id trailer (``parse.md``), the flat elements projection
(``elements.json``), provenance/params/identity metadata (``meta.json``),
and the claim ticket (``job.json``).
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

import typer

from . import attach, credentials, elements, items, oauth, store
from .config import DEFAULT_ENVIRONMENT, ENVIRONMENTS, ade_home, resolve_target
from .gateway import Gateway
from . import guarantee as lifecycle
from .guarantee import Guarantee, Tier
from .output import (
    EXIT_USAGE,
    ID_ONLY_FLAG,
    JSON_FLAG,
    emit,
    exit_with,
    set_id_only,
    tilde,
    timestamp,
)
from .ports import Ports

# The live parse's artifact set — written on completion, named in the
# summary's saved: line, and recorded in meta.json.
PARSE_ARTIFACTS = ["parse.json", "parse.md", "elements.json"]

# The registry's default alias — also what the parse-first phase of
# ``extract -d`` always runs (bare `parse -d` params, so the parse it
# mints is the exact item a plain `parse -d` would dedup against).
DEFAULT_MODEL = "dpt-3-pro-latest"


class Include(str, Enum):
    """Bulk parse artifacts a caller can ask for *on stdout* instead of
    reading them out of the store. Opt-in rather than always-on: a long
    document's markdown is megabytes, and the summary is what most runs
    want (F9)."""

    markdown = "markdown"
    elements = "elements"


def consume_parse_result(data: dict) -> list[dict]:
    """Consume every field the parse artifacts and summary read — the whole
    contract, before any write, so a result outside it (the API usually
    runs ahead of the CLI) fails whole: never a torn artifact set or a raw
    traceback (#31). Returns the elements projection."""
    element_records = elements.project(data)
    meta = data["metadata"]
    if not isinstance(data["markdown"], str):
        raise TypeError(f"markdown is {type(data['markdown']).__name__}, not str")
    _ = (
        meta["model_version"],
        meta["page_count"],
        len(meta["failed_pages"]),
        meta["billing"]["total_credits"],
        meta["billing"]["service_tier"],
    )
    return element_records


def _parse_pages(spec: str) -> list[int]:
    """Expand a ``--pages`` spec like ``1,3-5`` into the wire contract's
    1-indexed integer array (``options.pages``; values < 1 are rejected,
    numbers past the end of the document are silently ignored server-side)."""
    pages: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        start, dash, end = token.partition("-")
        if (
            not start.isdigit()
            or int(start) < 1
            or (dash and not end.isdigit())
            or (dash and int(end) < int(start))
        ):
            raise ValueError(
                f"Invalid --pages value {spec!r}: use 1-indexed pages and "
                "low-to-high ranges, e.g. '1,3-5'."
            )
        pages.extend(range(int(start), (int(end) if dash else int(start)) + 1))
    return pages


def parse(
    ctx: typer.Context,
    document: Path | None = typer.Option(
        None, "-d", "--document", exists=True, dir_okay=False, readable=True,
        help="Local document file to parse; pass exactly one of -d/--document or --document-url.",
    ),
    document_url: str | None = typer.Option(
        None, "--document-url",
        help="Document URL the server fetches (identity is the URL x params; "
        "a re-run dedups even if the remote content changed — --force refreshes).",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL, "--model", help="Parse model registry version."
    ),
    tier: Tier = typer.Option(
        Tier.priority, "--tier",
        help="Async lane: priority (full price, fast lane) or standard "
        "(half price, slower lane). The CLI defaults to priority.",
    ),
    pages: str | None = typer.Option(None, "--pages", help="1-indexed, e.g. '1,3-5'."),
    options_json: str | None = typer.Option(
        None, "--options",
        # The whole documented ParseOptions surface, inline: --help is where
        # an agent discovers what it can pass — a bare "pass-through" would
        # leave the keys undiscoverable without the OpenAPI spec at hand.
        help="Full ParseOptions pass-through as a JSON object, sent verbatim "
        "(the server rejects unknown keys with a 422). Keys as of this "
        "release — "
        "pages: 1-indexed integer array (default: all pages); "
        "atomic_grounding: bool (default true; false omits the per-line "
        "atomic_grounding field from every node); "
        "inline_markdown: bool (default false; true adds each node's own "
        "markdown slice inline); "
        "blocks.<type>.markdown: bool (default true; false suppresses that "
        "type's markdown; types: text, table, figure, marginalia, "
        "attestation, logo, scan_code, card); "
        "blocks.table.format: 'html' (default) or 'markdown'; "
        "password: always rejected (422) — decrypt PDFs before upload. "
        "Example: '{\"inline_markdown\": true, \"blocks\": {\"table\": "
        "{\"format\": \"markdown\"}}}'. "
        "Merges with --pages; giving pages in both is an error.",
    ),
    environment: str | None = typer.Option(
        None, "--env",
        help=f"Environment to run against: {', '.join(ENVIRONMENTS)} "
        "(default: $ADE_ENV, then production). Environments keep separate "
        "results — the job item id includes the environment.",
    ),
    wait: float = typer.Option(600.0, "--wait", help="Poll budget in seconds."),
    force: bool = typer.Option(
        False, "--force",
        help="Re-parse even if already parsed, or abandon an unreadable job "
        "for a fresh one (bills a new parse).",
    ),
    keep_copy: bool = typer.Option(
        False, "--keep-copy",
        help="--document-url only: also download the document into the "
        "job item (plain HTTP, no API credits) so page previews and "
        "crops render locally — fetched now, while the URL (often "
        "pre-signed) still works. Without it, the first `view`/`crop` "
        "fetches the copy instead, by which time a pre-signed URL may "
        "have expired.",
    ),
    include: list[Include] = typer.Option(
        [],
        "--include",
        help="Carry a bulk artifact in the payload instead of leaving it on "
        "disk: markdown (the parse markdown) or elements (the flat "
        "projection `find` searches). Repeatable.",
    ),
    as_json: bool = JSON_FLAG,
    id_only: bool = ID_ONLY_FLAG,
) -> None:
    """Ensure a document is parsed; persist all artifacts locally.

    The summary names the job item id every other verb takes: `find` it,
    `view` it, `extract` against it.
    """
    set_id_only(id_only)
    ports: Ports = ctx.obj
    home = ade_home()

    if (document is None) == (document_url is None):
        exit_with(
            {"error": "bad_source", "message": "Provide exactly one of -d/--document or --document-url."},
            "Provide exactly one of -d/--document or --document-url.",
            as_json=as_json,
            code=EXIT_USAGE,
        )
    if keep_copy and document_url is None:
        message = (
            "--keep-copy applies to --document-url parses; a local parse "
            "already renders previews from its file."
        )
        exit_with(
            {"error": "keep_copy_local_source", "message": message},
            message,
            as_json=as_json,
            code=EXIT_USAGE,
        )

    resolved = resolve_target(home, environment, as_json=as_json)
    active = credentials.require(home, resolved, as_json=as_json)

    gateway = Gateway(
        endpoint=resolved.endpoint,
        auth=oauth.bearer_auth(home, resolved, active, ports),
        transport=ports.transport,
        command="parse",
        org_id=active.org_id,
    )
    jobs = store.JobStore(home)
    # --options is the full ParseOptions object, passed through verbatim —
    # unknown keys are the server's 422 to reject, so new options work
    # without a CLI release. --pages merges in as a spelling convenience
    # for the same options.pages array (identical invocation, identical
    # job item id); pages given both ways is a conflict, not a precedence
    # — silent precedence could bill a page set the user didn't intend.
    options = {}
    if options_json is not None:
        try:
            options = json.loads(options_json)
        except json.JSONDecodeError:
            options = None  # fall through to the shared usage error
        if not isinstance(options, dict):
            message = (
                "Invalid --options value: expected a JSON object like "
                '\'{"atomic_grounding": false}\'.'
            )
            exit_with(
                {"error": "bad_options", "message": message},
                message,
                as_json=as_json,
                code=EXIT_USAGE,
            )
    if pages is not None:
        if "pages" in options:
            message = (
                "Give the page selection once: --pages and an --options "
                "'pages' key conflict."
            )
            exit_with(
                {"error": "bad_options", "message": message},
                message,
                as_json=as_json,
                code=EXIT_USAGE,
            )
        try:
            options["pages"] = _parse_pages(pages)
        except ValueError as err:
            exit_with(
                {"error": "bad_pages", "message": str(err)},
                str(err),
                as_json=as_json,
                code=EXIT_USAGE,
            )
    # Params are part of identity, not a cache key: tier included (it is
    # part of how this run was billed), pages inside options exactly as sent.
    params = {"model": model, "options": options, "tier": tier.value}

    document_upload: tuple[str, bytes] | None = None
    if document is not None:
        # One read serves both identity and upload — hashing and submitting
        # different bytes would file artifacts under the wrong job item id.
        document_bytes = document.read_bytes()
        document_upload = (document.name, document_bytes)
        identity = store.local_identity(document, document_bytes)
        source = str(document.resolve())
    else:
        assert document_url is not None  # guaranteed by the source check above
        identity = store.url_identity(document_url)
        source = document_url
    item_id = store.derive_id("parse", resolved.environment, identity, params)

    def emit_summary(
        data: dict,
        job_id: str,
        *,
        cached: bool,
        stored: bool = True,
        completed_at: float | None = None,
        copy_info: dict | None = None,
    ) -> None:
        meta = data["metadata"]
        billing = meta["billing"]
        failed = meta["failed_pages"]
        failed_note = (
            f"{len(failed)} failed: {', '.join(map(str, failed))}"
            if failed
            else "0 failed"
        )
        if cached:
            # Dedup-with-notice: the free path must say the run already
            # exists, when it completed, and how to consent to a re-bill.
            header = (
                f"already parsed — job item {item_id} "
                f"(completed {timestamp(completed_at)}); "
                "pass --force to re-parse"
                f"\n  source:  {source}"
            )
        else:
            header = (
                f"Parsed {source} -> job item {item_id}"
                + lifecycle.summary_note(cached=False, stored=stored)
            )
        store_dir = jobs.item_dir(item_id)
        ref = items.short_id(jobs, item_id)
        # What the job item id is for and where the artifacts landed (#34):
        # the store path, the artifact names, and runnable next commands
        # keyed by a short unambiguous id prefix. Skipped only when nothing
        # was saved.
        saved_line = (
            f"\n  saved:   {tilde(store_dir)}/  ({', '.join(PARSE_ARTIFACTS)})"
            if cached or stored
            else ""
        )
        next_line = (
            f"\n  next:    ade view {ref} --open"
            f"   ·   ade extract {ref} --schema <schema.json>"
        )
        copy_line = ""
        if copy_info is not None:
            if copy_info.get("error"):
                # The parse itself succeeded and billed; a failed copy is
                # a warning with the later remediation, never a failure.
                copy_line = (
                    f"\n  copy:    keep-copy failed — {copy_info['error']} "
                    f"(the parse succeeded; `ade view {ref}` retries the "
                    "fetch automatically)"
                )
            else:
                copy_line = (
                    f"\n  copy:    {copy_info['name']} saved into the job "
                    "item — page previews and crops render locally"
                )
        payload = {
            "status": "parsed",
            # The server-side run id — user-facing name for what the wire
            # (and the stored ticket/meta) still spell job_id: "run" names
            # the server work, "job item" names the local store unit.
            "run_id": job_id,
            "job_item_id": item_id,
            "environment": resolved.environment,
            "version": meta["model_version"],
            "credits": billing["total_credits"],
            "tier": billing["service_tier"],
            "page_count": meta["page_count"],
            "failed_pages": failed,
            "cached": cached,
            "stored": stored,
            "store_dir": str(store_dir),
            "artifacts": PARSE_ARTIFACTS,
        }
        if copy_info is not None:
            payload["kept_copy"] = not copy_info.get("error")
            if copy_info.get("error"):
                payload["keep_copy_error"] = copy_info["error"]
        # Asked-for bulk artifacts ride on stdout, so a caller never has to
        # reconstruct a store path to reach the result it just paid for.
        # Computed from the response in hand — the same bytes the artifacts
        # were written from, cached runs included.
        if Include.markdown in include:
            payload["markdown"] = data["markdown"]
        if Include.elements in include:
            payload["elements"] = elements.project(data)
        emit(
            payload,
            (
                header
                # The env line appears only off the beaten path — a named
                # non-default environment or an ADE_ENDPOINT override —
                # so the production-only user never reads boilerplate.
                + (
                    f"\n  env:     {resolved.endpoint_label}"
                    if resolved.environment != DEFAULT_ENVIRONMENT
                    or resolved.endpoint_source == "env"
                    else ""
                )
                + f"\n  run:     {job_id}"
                f"\n  model:   {meta['model_version']}"
                f"\n  pages:   {meta['page_count']} ({failed_note})"
                f"\n  credits: {billing['total_credits']} ({billing['service_tier']})"
                + saved_line
                + copy_line
                + next_line
            ),
            as_json=as_json,
        )

    def maybe_keep_copy() -> dict | None:
        """The --keep-copy download (#169), after the parse settled: while
        the URL — often pre-signed — still works. Idempotent (an attached
        copy is kept), and never fails the parse: the billable work
        already succeeded."""
        if not keep_copy:
            return None
        meta = jobs.read_json(item_id, "meta.json") or {}
        if attach.attached_file(jobs, item_id, meta) is not None:
            return {"kept": True, "name": meta.get("attached_source")}
        if not meta.get("source"):
            return {
                "kept": False,
                "error": "the store record is owned by a newer run",
            }
        try:
            name, _size = attach.download(
                jobs, item_id, meta,
                transport=ports.transport, now=ports.clock.now(),
            )
        except attach.AttachError as error:
            return {"kept": False, "error": error.message}
        return {"kept": True, "name": name}

    # The guarantee: this exact invocation (source x content x params — the
    # id) is served from disk free — unless the last attempt failed (a
    # reported failure resubmits fresh, never cache-hits). live_parse gates
    # generation consistency; params match by construction of the id.
    if not force and not lifecycle.failed_outstanding(jobs, item_id, "job.json", params):
        live = items.live_parse(jobs, item_id)
        if live is not None:
            stored_meta, stored_parse = live
            emit_summary(
                stored_parse,
                stored_meta["job_id"],
                cached=True,
                completed_at=stored_meta.get("completed_at"),
                copy_info=maybe_keep_copy(),
            )
            return

    data, job_id, stored = ensure_parsed(
        jobs=jobs,
        gateway=gateway,
        item_id=item_id,
        environment=resolved.environment,
        params=params,
        document_upload=document_upload,
        document_url=document_url,
        source=source,
        identity=identity,
        wait=wait,
        force=force,
        ports=ports,
        as_json=as_json,
        endpoint_label=resolved.endpoint_label,
    )
    emit_summary(data, job_id, cached=False, stored=stored, copy_info=maybe_keep_copy())


def ensure_parsed(
    *,
    jobs: store.JobStore,
    gateway: Gateway,
    item_id: str,
    environment: str,
    params: dict,
    document_upload: tuple[str, bytes] | None,
    document_url: str | None,
    source: str,
    identity: dict,
    wait: float,
    force: bool,
    ports: Ports,
    as_json: bool,
    endpoint_label: str,
) -> tuple[dict, str, bool]:
    """Run one parse job item's guarantee to completion and publish its
    artifacts: claim → submit → poll → consume → publish, with every
    non-completed outcome exiting through the shared lifecycle. Returns
    ``(raw ParseResponse, job_id, stored)``. Shared by ``parse`` and the
    parse-first phase of ``extract -d`` — both mint the exact same job
    item, which is what makes every parse the CLI runs reusable."""
    guarantee = Guarantee(
        store=jobs,
        item_id=item_id,
        kind="parse",
        ticket_name="job.json",
        params=params,
        tier=params["tier"],
        source=source,
        wait=wait,
        clock=ports.clock,
        as_json=as_json,
        context={"job_item_id": item_id},
        noun="Parse",
        endpoint_label=endpoint_label,
        environment=environment,
        post=lambda ticket: gateway.submit_parse(
            document=document_upload,
            document_url=document_url,
            model=params["model"],
            service_tier=params["tier"],
            options=params["options"] or None,
        ),
        poll=gateway.get_parse_job,
        fresh=force,
        stderr_tty=ports.stderr_is_tty(),
        interrupted_no_job_hint=(
            "Interrupted before a run was recorded; re-run the same "
            "command to continue. If the interrupt landed mid-submit, "
            "the server may have accepted the run anyway and the re-run "
            "may bill a second parse of the same bytes (no idempotency "
            "key exists yet — see the filed platform ask)."
        ),
    )
    outcome = guarantee.ensure()
    data, job_id = outcome.result, outcome.job_id
    try:
        element_records = consume_parse_result(data)  # whole contract, pre-write
        meta = data["metadata"]
    except (AttributeError, KeyError, IndexError, TypeError) as err:
        guarantee.unreadable_result(outcome, lifecycle.schema_problem(data, err))

    def write_artifacts() -> None:
        # Within the set, meta.json is written last as the commit record
        # (the cache serves only when its job_id matches parse.json's).
        jobs.write_json(item_id, "parse.json", data)  # raw response, verbatim
        jobs.write_text(item_id, "parse.md", data["markdown"])  # doc_id trailer
        # Derived index, job_id-stamped so readers can tell it belongs
        # to this generation; recomputable from parse.json alone.
        jobs.write_json(
            item_id,
            "elements.json",
            {"job_id": job_id, "elements": element_records},
        )
        jobs.write_json(
            item_id,
            "meta.json",
            {
                "job_item_id": item_id,
                "kind": "parse",
                "source": source,
                # The environment this job ran in: part of the id, and what
                # an extract over this item must inherit (the server-side
                # job id below only exists there).
                "environment": environment,
                # The identity components, reusable verbatim: an extract of
                # this item derives its own id from the same source/content.
                "identity": identity,
                "state": "parsed",
                "params": params,
                "job_id": job_id,
                "model_version": meta["model_version"],
                "page_count": meta["page_count"],
                "failed_pages": meta["failed_pages"],
                # Denormalized for the sidebar scan: reading it here keeps
                # history.js off the (large) raw responses.
                "credits": meta["billing"]["total_credits"],
                "completed_at": ports.clock.now(),
                "artifacts": PARSE_ARTIFACTS,
            },
        )

    stored = guarantee.publish(outcome, write_artifacts)
    return data, job_id, stored
