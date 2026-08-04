"""``extract`` — the guarantee: ensure a schema extraction exists.

Runs on the shared lifecycle in ``guarantee.py`` (claim tickets, wait
semantics, interrupt posture, tiers, pending-as-non-error all inherited).
Every extraction is its own top-level job item under ``jobs/``, keyed like
parse: verb × source × content × extract params (schema + model + options
— plus, for the JOB_ITEM_ID form, the referenced parse item: parse *variants*
of one document must mint sibling extractions, never re-run one in
place). Input is exactly one of:

- ``JOB_ITEM_ID`` — a completed parse job item (or unambiguous prefix); the
  extract item *references* it via ``parse/ref.json`` — parse artifacts
  are never copied, so there is one copy of ground truth and staleness
  stays a job_id comparison.
- ``-d FILE`` — extract by document path: reuse the latest completed
  parse job of this path+content (any params, newest ``completed_at``
  wins; logged and referenced exactly like the JOB_ITEM_ID form). If none
  exists, run a **standalone parse job first** — bare ``parse -d``
  params, a normal top-level parse item, exactly as if the user had run
  ``parse -d`` — then the extract referencing it: two billable jobs,
  both itemised in one summary. Every parse the CLI runs is a reusable
  job item, so repeated ``extract -d`` on a never-parsed document bills
  the parse exactly once (decision 10, as revised 2026-07-21).
- ``--markdown FILE`` / ``--markdown-url URL`` — bring-your-own markdown,
  copied into the item as ``markdown.md`` (spans index exactly those
  bytes; for URLs the response's echoed markdown is materialized — the
  CLI never had a local file). No parse edge: evidence is spans-only.

Markdown above the staging threshold rides as a multipart file part (the
gateway stages the upload internally). Artifacts on completion: the raw
V2ExtractResult (``extract.json``, per-field ``{value, ranges}`` metadata
verbatim — null ranges are reported as ungroundable when the value is
non-empty, quietly as empty otherwise), the derived
field→box join (``evidence.json``), and the commit record (``meta.json``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import typer

from . import credentials, elements, evidence, items, oauth, store
from .config import DEFAULT_ENVIRONMENT, ENVIRONMENTS, ade_home, resolve_target
from .gateway import Gateway
from .history import resolve_or_exit
from . import guarantee as lifecycle
from .guarantee import Guarantee, Tier
from .output import (
    EXIT_FAILED,
    EXIT_USAGE,
    ID_ONLY_FLAG,
    JSON_FLAG,
    emit,
    exit_with,
    set_id_only,
    tilde,
    timestamp,
)
from .parse import DEFAULT_MODEL as DEFAULT_PARSE_MODEL
from .parse import ensure_parsed
from .ports import Ports

# One extract item's artifact set — written on completion, named in the
# summary's saved: line, and recorded in its meta.json.
EXTRACT_ARTIFACTS = ["extract.json", "evidence.json"]

# Markdown at or below this rides inline in the submit body; above it, the
# bytes ride as a multipart FILE part the gateway stages internally.
INLINE_MARKDOWN_MAX_BYTES = 1 << 20  # 1 MiB


def _load_schema(spec: str, *, as_json: bool) -> dict:
    """``--schema`` accepts a JSON Schema file path or an inline JSON
    object; anything else is a usage error."""
    path = Path(spec)
    try:
        is_file = path.is_file()
    except OSError:
        # Probing an inline JSON object longer than the filesystem's name
        # limit makes stat() raise (ENAMETOOLONG) instead of returning
        # False — a spec the OS cannot even stat is never a path (#143).
        is_file = False
    if is_file:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            # OSError: permissions and friends; UnicodeError: the file
            # exists but is not UTF-8 text — both are "not a readable
            # schema file", and both must exit structured (#143).
            exit_with(
                {
                    "error": "bad_schema",
                    "message": f"--schema file {spec} is unreadable: {error}",
                },
                f"--schema {spec!r} names a file that cannot be read "
                f"({error}); fix the file or pass an inline JSON object.",
                as_json=as_json,
                code=EXIT_USAGE,
            )
    else:
        text = spec
    try:
        schema = json.loads(text)
    except json.JSONDecodeError:
        schema = None
    if not isinstance(schema, dict):
        exit_with(
            {
                "error": "bad_schema",
                "message": "--schema must be a JSON Schema file or an inline JSON object.",
            },
            f"--schema {spec!r} is neither a readable file nor an inline "
            "JSON object; pass a JSON Schema file path or a JSON object literal.",
            as_json=as_json,
            code=EXIT_USAGE,
        )
    return schema


def _has_extractable_fields(schema: object) -> bool:
    """Whether a schema defines anything an extraction could produce:
    non-empty ``properties``/``patternProperties`` anywhere reachable
    through local composition (``allOf``/``anyOf``/``oneOf``/``items``).
    A ``$ref`` passes — it may point at real fields the CLI cannot
    resolve locally, and a false block would be worse than a billed
    empty run. Empty composition shells (``{"allOf": []}``,
    ``{"items": {}}``) define nothing and do not pass."""
    if not isinstance(schema, dict):
        return False
    if schema.get("properties") or schema.get("patternProperties"):
        return True
    if schema.get("$ref"):
        return True
    items = schema.get("items")
    branches = items if isinstance(items, list) else [items]
    for key in ("allOf", "anyOf", "oneOf"):
        value = schema.get(key)
        if isinstance(value, list):
            branches = [*branches, *value]
    return any(_has_extractable_fields(branch) for branch in branches)


def _require_extractable_schema(schema: dict, *, as_json: bool) -> None:
    """Refuse a schema with nothing to extract *before* any billable step
    (the same local-validation posture as the --pages/--options conflict):
    an empty ``properties`` map is valid JSON Schema, but the server still
    processes the whole document with the LLM and bills for it while
    returning zero fields."""
    if _has_extractable_fields(schema):
        return
    message = (
        "The schema defines no fields to extract (empty or missing "
        "'properties'). The request was not sent: the server would process "
        "the whole document, bill credits, and return an empty extraction. "
        "Add at least one property to the schema."
    )
    exit_with(
        {"error": "empty_schema", "message": message},
        message,
        as_json=as_json,
        code=EXIT_USAGE,
    )


def _schema_sha256(schema: dict) -> str:
    """Full sha256 of the schema's canonical JSON form — its stand-in in
    the recorded join params (the schema itself is recorded verbatim in
    meta.json). Full-length so the field's name stays honest on disk."""
    return hashlib.sha256(store.canonical_params(schema).encode()).hexdigest()


# The parse-first phase of `extract -d` always runs bare `parse -d` params
# — a fresh parse triggered by extract must land on the exact job item a
# plain `parse -d` mints, so either verb resumes or reuses the other's work.
DEFAULT_PARSE_PARAMS = {
    "model": DEFAULT_PARSE_MODEL,
    "options": {},
    "tier": Tier.priority.value,
}


def _parse_bill(parse_item_id: str, parse_data: dict) -> dict:
    """The itemised bill of the parse-first job — the second billable job a
    fresh ``extract -d`` invocation carries, named in the summary next to
    the extraction's own bill."""
    meta = parse_data["metadata"]
    return {
        "job_item_id": parse_item_id,
        # User-facing name of the server-side id (the wire spells it job_id).
        "run_id": meta["job_id"],
        "version": meta["model_version"],
        "credits": meta["billing"]["total_credits"],
        "tier": meta["billing"]["service_tier"],
    }


def _warning_text(warning: object) -> str:
    """One server warning as summary text: its message when it carries one,
    else the whole record, compactly. The wire shape is list[dict] with no
    pinned keys, so unknown shapes print rather than vanish."""
    message = warning.get("message") if isinstance(warning, dict) else None
    if isinstance(message, str):
        return message
    return json.dumps(warning, ensure_ascii=False)


def _hanging_lines(label: str, lines: list[str]) -> str:
    """Summary lines under one label, hanging-indented to the summary's
    value column; empty input is no line at all."""
    if not lines:
        return ""
    head, *rest = lines
    return f"\n  {label:<10}{head}" + "".join(
        f"\n{' ' * 12}{line}" for line in rest
    )


def _idempotency_key(item_id: str, ticket: dict) -> str:
    """Derived from the claim generation (see the filed platform ask): the
    same claim — including one inherited from a crashed submit — always
    resubmits with the same key, so the server can attach the retry to a
    job it already accepted instead of minting a duplicate."""
    generation = ticket.get("claimed_at") or ticket.get("submitted_at") or 0
    seed = f"{item_id}:{generation}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def extract(
    ctx: typer.Context,
    job_id_token: str | None = typer.Argument(
        None,
        metavar="[JOB_ITEM_ID]",
        help="A completed parse job item id (or unambiguous prefix).",
    ),
    schema_spec: str = typer.Option(
        ..., "--schema", help="JSON Schema file or inline JSON object."
    ),
    document: Path | None = typer.Option(
        None, "-d", "--document", exists=True, dir_okay=False, readable=True,
        help="Extract by document path: reuse the latest completed parse of "
        "this path+content, else run a standalone parse job first — as if "
        "you had run `parse -d` — then extract (two billable jobs, both "
        "itemised).",
    ),
    markdown: Path | None = typer.Option(
        None, "--markdown", exists=True, dir_okay=False, readable=True,
        help="Escape hatch: extract from markdown that did not come from parse.",
    ),
    markdown_url: str | None = typer.Option(
        None, "--markdown-url",
        help="Escape hatch: extract from markdown fetched from a URL "
        "server-side (the response's echoed markdown is stored).",
    ),
    model: str = typer.Option(
        "extract-latest", "--model", help="Extract model registry version."
    ),
    tier: Tier = typer.Option(
        Tier.priority, "--tier",
        help="Async lane: priority (full price, fast lane) or standard "
        "(half price, slower lane). The CLI defaults to priority.",
    ),
    strict: bool = typer.Option(
        False, "--strict",
        help="v2 ExtractOptions pass-through: the server rejects (422) schemas "
        "with fields the model cannot extract instead of skipping them.",
    ),
    environment: str | None = typer.Option(
        None, "--env",
        help=f"Environment to run against: {', '.join(ENVIRONMENTS)} "
        "(default: $ADE_ENV, then production). The JOB_ITEM_ID form inherits the "
        "parse item's environment instead — its server-side parse job only "
        "exists there — and a conflicting --env is refused.",
    ),
    wait: float = typer.Option(600.0, "--wait", help="Poll budget in seconds."),
    force: bool = typer.Option(
        False, "--force",
        help="Re-extract even if already extracted, or abandon an unreadable "
        "job for a fresh one (bills a new extract).",
    ),
    as_json: bool = JSON_FLAG,
    id_only: bool = ID_ONLY_FLAG,
) -> None:
    """Ensure an extraction exists for a parse job item (or bring-your-own
    markdown); persist the result as its own job item.

    The schema-shaped result rides in the payload (`extraction`) with its
    per-field evidence; `view JOB_ITEM_ID` renders the same join on the page,
    and `find`/`crop` on the referenced parse reach the cited elements.
    """
    set_id_only(id_only)
    ports: Ports = ctx.obj
    home = ade_home()

    sources = [
        s for s in (job_id_token, document, markdown, markdown_url) if s is not None
    ]
    if len(sources) != 1:
        message = (
            "Provide exactly one of JOB_ITEM_ID, -d/--document, --markdown, "
            "or --markdown-url."
        )
        exit_with(
            {"error": "bad_source", "message": message},
            message,
            as_json=as_json,
            code=EXIT_USAGE,
        )

    schema = _load_schema(schema_spec, as_json=as_json)
    _require_extractable_schema(schema, as_json=as_json)

    # Resolves --env → ADE_ENV → production, and validates the flag before
    # any billable step. The JOB_ITEM_ID branch below re-resolves with the parse
    # item's own environment: the item pins the target (its server-side
    # parse job id exists nowhere else), overriding ambient ADE_ENV, while
    # an explicit conflicting --env is refused loudly.
    resolved = resolve_target(home, environment, as_json=as_json)
    jobs = store.JobStore(home)

    # Resolve the markdown source: a completed parse job item (the primary
    # path — its doc_id trailer links extract back to parse server-side),
    # a document path (reuse the latest parse, else run a standalone parse
    # job first), or bring-your-own markdown copied into this item.
    markdown_text: str | None = None
    parse_job_id: str | None = None
    parse_item_id: str | None = None
    element_records: list[dict] | None = None
    markdown_bytes: bytes | None = None
    reused_parse_meta: dict | None = None  # the reuse the summary logs
    parse_first: tuple[str, bytes] | None = None  # fresh-path upload
    parsed_first: dict | None = None  # the itemised parse-first bill
    if job_id_token is not None:
        parse_item_id = resolve_or_exit(jobs, job_id_token, as_json=as_json)
        record = items.item_record(jobs, parse_item_id)
        if record["kind"] != "parse":
            exit_with(
                {
                    "error": "not_a_parse_item",
                    "job_item_id": parse_item_id,
                    "kind": record["kind"],
                    "message": "extract takes a parse job item id.",
                },
                f"Job item {parse_item_id} is an extract item; extract takes "
                "a parse job item id (run `ade history list`).",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        live = items.live_parse(jobs, parse_item_id)
        if live is None:
            exit_with(
                {
                    "error": "not_parsed",
                    "job_item_id": parse_item_id,
                    "state": record["state"],
                    "message": "No completed parse; re-run `ade parse` to finish it.",
                },
                f"Job item {parse_item_id} has no completed parse "
                f"(state: {record['state']}); re-run `ade parse` to finish it.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        live_meta, live_response = live
        item_environment = live_meta.get("environment", DEFAULT_ENVIRONMENT)
        if environment is not None and environment != item_environment:
            exit_with(
                {
                    "error": "environment_mismatch",
                    "job_item_id": parse_item_id,
                    "item_environment": item_environment,
                    "environment": environment,
                    "message": "The parse item pins its environment.",
                },
                f"Job item {parse_item_id} was parsed in the "
                f"{item_environment} environment; its extract must run "
                f"there too (the referenced parse job exists nowhere else). "
                f"Drop --env {environment}, or re-parse with "
                f"`ade parse --env {environment}`.",
                as_json=as_json,
                code=EXIT_USAGE,
            )
        resolved = resolve_target(home, item_environment, as_json=as_json)
        markdown_text = live_response["markdown"]  # ends with the doc_id trailer
        parse_job_id = live_meta["job_id"]  # the parse generation fingerprint
        # Projected from the exact response the spans will index — a forced
        # re-parse completing mid-extraction can never slip another
        # generation's elements into this run's evidence join.
        element_records = elements.project(live_response)
        source = live_meta.get("source") or parse_item_id
        # The extract item id shares the parse item's identity components:
        # same document, different verb and params.
        identity = live_meta["identity"]
    elif document is not None:
        # One read serves both identity and (if it comes to a parse-first
        # job) upload — hashing and submitting different bytes would file
        # artifacts under the wrong job item id.
        document_bytes = document.read_bytes()
        identity = store.local_identity(document, document_bytes)
        source = str(document.resolve())
        found = items.latest_parse(jobs, identity, resolved.environment)
        if found is not None:
            # Reused and referenced exactly like the JOB_ITEM_ID form — no parse
            # billed; the reuse is logged in the summary.
            parse_item_id, live_meta, live_response = found
            markdown_text = live_response["markdown"]
            parse_job_id = live_meta["job_id"]
            element_records = elements.project(live_response)
            reused_parse_meta = live_meta
        else:
            # No parse to reuse: this invocation runs a standalone parse
            # job first (bare `parse -d` params — decision 10, revised) and
            # the extract references it. The parse item id is derivable now,
            # so it joins the extract identity like any referenced parse.
            parse_first = (document.name, document_bytes)
            parse_item_id = store.derive_id(
                "parse", resolved.environment, identity, DEFAULT_PARSE_PARAMS
            )
    elif markdown is not None:
        markdown_bytes = markdown.read_bytes()
        try:
            markdown_text = markdown_bytes.decode("utf-8")
        except UnicodeDecodeError:
            exit_with(
                {"error": "bad_markdown", "message": f"{markdown} is not UTF-8 text."},
                f"--markdown {markdown} is not UTF-8 text.",
                as_json=as_json,
                code=EXIT_USAGE,
            )
        identity = store.local_identity(markdown, markdown_bytes)
        source = str(markdown.resolve())
    else:
        assert markdown_url is not None  # guaranteed by the source check above
        identity = store.url_identity(markdown_url)
        source = markdown_url

    # Identity params — schema verbatim (canonicalized by the hash), model,
    # options: same document + same schema + same params ⇒ same item.
    options = {"strict": True} if strict else {}
    id_params: dict = {"schema": schema, "model": model, "options": options}
    if parse_item_id is not None:
        # Two parse *variants* of one document share source and content
        # hashes; without the parse linkage in identity their extractions
        # would collide on one id and silently re-run each other in place —
        # against "variants coexist; nothing is silently replaced". Which
        # parse run fed the extraction is part of the invocation.
        id_params["parse_job_item_id"] = parse_item_id
    item_id = store.derive_id("extract", resolved.environment, identity, id_params)

    active = credentials.require(home, resolved, as_json=as_json)
    gateway = Gateway(
        endpoint=resolved.endpoint,
        auth=oauth.bearer_auth(home, resolved, active, ports),
        transport=ports.transport,
        command="extract",
    )

    if parse_first is not None:
        # The parse-first phase: a normal top-level parse item, exactly as
        # if the user had run `parse -d` — resumable by either verb,
        # reusable by every later scan. Runs before the cache key forms:
        # the extraction joins this parse's generation. extract's --force
        # consents to re-bill the extraction only, so the parse guarantee
        # here is never forced — its own --force lives on `parse`.
        assert parse_item_id is not None  # set beside parse_first, always
        if not as_json:
            typer.echo(
                f"no reusable parse for {source}; running a standalone "
                f"parse first (job item {parse_item_id}; bills a parse)",
                err=True,
            )
        parse_data, parse_job_id, _ = ensure_parsed(
            jobs=jobs,
            gateway=gateway,
            item_id=parse_item_id,
            environment=resolved.environment,
            params=DEFAULT_PARSE_PARAMS,
            document_upload=parse_first,
            document_url=None,
            source=source,
            identity=identity,
            wait=wait,
            force=False,
            ports=ports,
            as_json=as_json,
            endpoint_label=resolved.endpoint_label,
        )
        parsed_first = _parse_bill(parse_item_id, parse_data)
        markdown_text = parse_data["markdown"]
        # Projected from the exact response the spans will index, same as
        # the reuse path.
        element_records = elements.project(parse_data)

    # The join/cache key recorded on the ticket. parse_job_id is
    # load-bearing: spans index the markdown of the parse *generation* that
    # produced them, so a forced re-parse (new job_id, same parse item)
    # mismatches stored params — the extraction is stale and re-extracts in
    # place. schema_sha256 stands in for the schema itself (recorded
    # verbatim in meta.json); tier deliberately stays out — it changes the
    # bill, not the result, so a running job is joined whatever lane it was
    # sent to.
    params = {
        "schema_sha256": _schema_sha256(schema),
        "model": model,
        "options": options,
        "parse_job_id": parse_job_id,
    }

    def emit_summary(
        data: dict,
        job_id: str,
        evidence_doc: dict,
        *,
        cached: bool,
        stored: bool = True,
        completed_at: float | None = None,
    ) -> None:
        meta = data["metadata"]
        billing = meta.get("billing") or {
            "service_tier": tier.value,
            "total_credits": meta.get("credit_usage", 0),
        }
        # The deployed API reports the resolved model as model_version (same
        # as parse); the openapi.json snapshot says version. Read both —
        # contract-drift ask filed rather than trusting either alone.
        version = meta.get("model_version") or meta.get("version")
        # The partial-success signals (#118): both ride every current poll
        # body — a partial extraction still completes with HTTP 200, and
        # schema_violation_error says which schema fields were skipped
        # (strict=false). Same posture as the playground UI: an advisory
        # warning on a successful run, never a failure. .get() keeps
        # responses stored by older CLI versions readable.
        violation = data.get("schema_violation_error")
        server_warnings = data.get("warnings") or []
        fields = evidence_doc["fields"]
        evidence_payload = {"kind": evidence_doc["kind"], "fields": fields}
        if "reason" in evidence_doc:
            # The degradation's cause (markdown_doc | parse_replaced) rides
            # along wherever the kind does — labeled, never implied.
            evidence_payload["reason"] = evidence_doc["reason"]
        ungroundable = [f["field"] for f in fields if f.get("ungroundable")]
        empty_fields = [f["field"] for f in fields if f.get("empty")]
        if cached:
            # Dedup-with-notice, like parse: the free path must say the run
            # already exists, when it completed, and how to consent to a
            # re-bill.
            header = (
                f"already extracted — job item {item_id} "
                f"(completed {timestamp(completed_at)}); "
                "pass --force to re-extract"
                f"\n  source:   {source}"
            )
        else:
            header = f"Extracted {source} -> job item {item_id}" + (
                lifecycle.summary_note(cached=False, stored=stored)
            )
        # How this invocation got its parse — reused (no parse billed) or
        # run first (a second bill, itemised): stated wherever the summary
        # is served, cached hits included.
        parse_line = ""
        if reused_parse_meta is not None:
            parse_line = (
                f"\n  parse:    reused parse job item {parse_item_id} "
                f"(model {reused_parse_meta['model_version']}, completed "
                f"{timestamp(reused_parse_meta.get('completed_at'))})"
            )
        elif parsed_first is not None:
            parse_line = (
                f"\n  parse:    parsed first — job item "
                f"{parsed_first['job_item_id']} · run {parsed_first['run_id']} "
                f"· {parsed_first['version']} · {parsed_first['credits']} "
                f"credits ({parsed_first['tier']}) — reusable, like any "
                "parse job item"
            )
        # Ungroundable is the alarm (a real value whose evidence could not
        # be located); empty-valued fields are expected — nothing to ground
        # an empty string to — and read quietly after it (F5).
        grounding_note = (
            f"{len(ungroundable)} ungroundable: {', '.join(ungroundable)}"
            if ungroundable
            else ("all non-empty fields grounded" if empty_fields else "all grounded")
        )
        if empty_fields:
            grounding_note += f"; {len(empty_fields)} empty — no value to ground"
        if evidence_doc["kind"] == "grounded":
            evidence_note = "grounded (field->box join from stored artifacts)"
        elif evidence_doc.get("reason") == "parse_replaced":
            evidence_note = "spans-only (the parse was replaced; this generation's grounding is gone)"
        else:
            evidence_note = "spans-only (bring-your-own markdown has no grounding to join)"
        store_dir = jobs.item_dir(item_id)
        saved_line = (
            f"\n  saved:    {tilde(store_dir)}/  ({', '.join(EXTRACT_ARTIFACTS)})"
            if cached or stored
            else ""
        )
        next_cmds = ["ade history list --json"]
        if parse_item_id is not None:
            # The referenced parse item's viewer renders this extraction as
            # a layer; a bring-your-own-markdown item has no parse and never
            # will, so hinting at the viewer would hint at an error.
            next_cmds.append(f"ade view {items.short_id(jobs, parse_item_id)} --open")
        next_line = "\n  next:     " + "   ·   ".join(next_cmds)
        payload = {
            "status": "extracted",
            # The server-side run id — user-facing name for what the wire
            # (and the stored ticket/meta) still spell job_id.
            "run_id": job_id,
            "job_item_id": item_id,
            "environment": resolved.environment,
            "version": version,
            "credits": billing["total_credits"],
            "tier": billing["service_tier"],
            # The result itself, schema-shaped, on stdout: what the run was
            # for. Reading extract.json out of the store is an option, never
            # the requirement (F9).
            "extraction": data.get("extraction"),
            "fields": len(fields),
            "ungroundable": ungroundable,
            "empty_fields": empty_fields,
            # Always present, so a scripter can gate on them without
            # probing: null / [] on a clean run.
            "schema_violation_error": violation,
            "warnings": server_warnings,
            "evidence": evidence_payload,
            "cached": cached,
            "stored": stored,
            "store_dir": str(store_dir),
            "artifacts": EXTRACT_ARTIFACTS,
        }
        if parse_item_id is not None:
            payload["parse_job_item_id"] = parse_item_id
        if reused_parse_meta is not None:
            payload["reused_parse"] = {
                "job_item_id": parse_item_id,
                "model_version": reused_parse_meta["model_version"],
                "completed_at": reused_parse_meta.get("completed_at"),
            }
        if parsed_first is not None:
            payload["parsed_first"] = parsed_first
        emit(
            payload,
            (
                header
                # Same posture as parse: named only off the beaten path.
                + (
                    f"\n  env:      {resolved.endpoint_label}"
                    if resolved.environment != DEFAULT_ENVIRONMENT
                    or resolved.endpoint_source == "env"
                    else ""
                )
                + parse_line
                + f"\n  run:      {job_id}"
                f"\n  model:    {version}"
                f"\n  fields:   {len(fields)} ({grounding_note})"
                # The alarm above the fold: a run billed at the partial
                # tier must never read as a clean success — shown whole,
                # never reduced to a count or a first line (#118).
                + _hanging_lines("partial:", violation.splitlines() if violation else [])
                + _hanging_lines(
                    "warnings:", [_warning_text(w) for w in server_warnings]
                )
                + f"\n  evidence: {evidence_note}"
                f"\n  credits:  {billing['total_credits']} ({billing['service_tier']})"
                + saved_line
                + next_line
            ),
            as_json=as_json,
        )

    # The guarantee: this exact invocation × parse-generation match is
    # served from disk free — unless the last attempt for these params
    # failed (a reported failure resubmits fresh, never cache-hits).
    if not force and not lifecycle.failed_outstanding(jobs, item_id, "job.json", params):
        live_ex = items.live_extract(jobs, item_id)
        if live_ex is not None and live_ex[0].get("params") == params:
            stored_meta, stored_response = live_ex
            evidence_doc = evidence.for_extraction(jobs, item_id, stored_meta, stored_response)
            emit_summary(
                stored_response,
                stored_meta["job_id"],
                evidence_doc,
                cached=True,
                completed_at=stored_meta.get("completed_at"),
            )
            return

    def post(ticket: dict) -> httpx.Response:
        inline: str | None = markdown_text
        markdown_upload: tuple[str, bytes] | None = None
        if inline is not None and len(inline.encode()) > INLINE_MARKDOWN_MAX_BYTES:
            # Transparently staged: above the threshold the markdown rides
            # as a multipart FILE part — the contract's large-input path
            # (the gateway stages the upload internally; the public request
            # takes no *_ref fields). Same guarantee, same artifacts, any
            # size; the lease heartbeat covers however long it takes.
            markdown_upload = ("markdown.md", inline.encode())
            inline = None
        return gateway.submit_extract(
            schema=schema,
            markdown=inline,
            markdown_upload=markdown_upload,
            markdown_url=markdown_url,
            model=model,
            service_tier=tier.value,
            options=options or None,
            idempotency_key=_idempotency_key(item_id, ticket),
        )

    guarantee = Guarantee(
        store=jobs,
        item_id=item_id,
        kind="extract",
        ticket_name="job.json",
        params=params,
        tier=tier.value,
        source=source,
        wait=wait,
        clock=ports.clock,
        as_json=as_json,
        context={"job_item_id": item_id},
        noun="Extract",
        endpoint_label=resolved.endpoint_label,
        environment=resolved.environment,
        post=post,
        poll=gateway.get_extract_job,
        fresh=force,
        stderr_tty=ports.stderr_is_tty(),
        interrupted_no_job_hint=(
            "Interrupted before a run was recorded; re-run the same command "
            "to continue. The resubmit carries the same idempotency key, so "
            "the server can attach it to a run it already accepted instead "
            "of billing a duplicate (platform support pending — see the "
            "filed ask)."
        ),
    )
    outcome = guarantee.ensure()
    data, job_id = outcome.result, outcome.job_id
    try:
        # Consume before any write (same posture as parse #31): the fields
        # the artifacts and summary read (mirroring emit_summary and
        # write_artifacts exactly), and the evidence join, must all succeed
        # before a byte hits disk.
        meta = data["metadata"]
        billing = meta.get("billing") or {
            "service_tier": tier.value,
            "total_credits": meta.get("credit_usage", 0),
        }
        _ = (
            billing["total_credits"],
            billing["service_tier"],
            meta.get("model_version") or meta.get("version"),
        )
        if markdown_url is not None and not isinstance(data.get("markdown"), str):
            # The URL form's markdown.md materializes from the response's
            # echo — the CLI never had a local file. A missing or non-string
            # echo must fail whole, never persist an empty input contract
            # under spans that indexed different bytes.
            raise TypeError(
                "markdown echo is "
                f"{type(data.get('markdown')).__name__}, not str"
            )
        evidence_doc = evidence.build(
            data["extraction_metadata"],
            element_records,
            job_id=job_id,
            parse_job_id=parse_job_id,
        )
    except (AttributeError, KeyError, IndexError, TypeError) as err:
        guarantee.unreadable_result(outcome, lifecycle.schema_problem(data, err))

    def write_artifacts() -> None:
        # extract.json is the raw response, verbatim ground truth (per-field
        # {value, ranges} metadata included — spanless fields keep their
        # null ranges and are derived as ungroundable/empty at read time, never
        # edited in). evidence.json is the derived field->box join, always
        # recomputable from the raw artifacts. meta.json is written last as
        # the commit record.
        jobs.write_json(item_id, "extract.json", data)
        jobs.write_json(item_id, "evidence.json", evidence_doc)
        if parse_item_id is not None:
            # The dependency edge, never a copy: one copy of ground truth
            # lives in the parse item; history clear cascades over this ref
            # so it can never dangle.
            ref_record = {"job_item_id": parse_item_id, "parse_job_id": parse_job_id}
            if parsed_first is not None:
                # Provenance, not ownership: this extract invocation created
                # the parse (direct `extract -d`, no reusable parse existed).
                # Data-only — clearing the extract leaves the parse alive.
                ref_record["direct"] = True
            jobs.write_json(item_id, "parse/ref.json", ref_record)
        else:
            # Bring-your-own markdown is copied in: the markdown *is* the
            # extraction's input contract — spans index exactly these bytes.
            # For --markdown-url the response's echoed markdown is
            # materialized (validated as a string before any write).
            copied = (
                markdown_bytes.decode("utf-8")
                if markdown_bytes is not None
                else data["markdown"]
            )
            jobs.write_text(item_id, "markdown.md", copied)
        jobs.write_json(
            item_id,
            "meta.json",
            {
                "job_item_id": item_id,
                "kind": "extract",
                "source": source,
                # Part of the id; matches the referenced parse item's by
                # construction (the JOB_ITEM_ID form inherits it).
                "environment": resolved.environment,
                "identity": identity,
                "state": "extracted",
                "params": params,
                "schema": schema,  # verbatim, for listing and re-runs
                "job_id": job_id,
                # Deployed wire spelling first (model_version, as on parse);
                # openapi.json's `version` kept as fallback — drift ask filed.
                "version": data["metadata"].get("model_version")
                or data["metadata"].get("version"),
                # Denormalized for the sidebar scan (same posture as parse).
                "credits": (data["metadata"].get("billing") or {}).get(
                    "total_credits", data["metadata"].get("credit_usage")
                ),
                # The partial-success signals (#118), denormalized so
                # history list / the sidebar can show the state without
                # opening extract.json (which keeps them verbatim).
                "schema_violation_error": data.get("schema_violation_error"),
                "warnings": len(data.get("warnings") or []),
                "completed_at": ports.clock.now(),
                "artifacts": EXTRACT_ARTIFACTS,
            },
        )

    stored = guarantee.publish(outcome, write_artifacts)
    emit_summary(data, job_id, evidence_doc, cached=False, stored=stored)
