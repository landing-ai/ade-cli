"""``view`` — one explorable HTML artifact per job item, with a history
sidebar shared across all viewers.

One generic template, per-item snapshots, built lazily: ``view`` stamps the
job item's stored artifacts into the template on demand and records the
fingerprint it was built from, rebuilding only when the store (or the
source, or the build params) changed. The document payload is a snapshot by
design and works from ``file://``; the first ``PAGE_CAP`` pages embed
inline (small documents stay one shareable file) and every page beyond
loads on demand from ``pages-N.js`` sidecar chunks in the store.

New-layout command (the job-item store): ``view JOB_ITEM_ID`` where JOB_ITEM_ID is a
job item id or unambiguous prefix. A parse item's viewer holds the parse
ONLY — a document can carry many extractions and the viewer can't guess
which one the user means. Each extract item owns its viewer: a LIGHT
artifact rendering its extraction over the parse it references (imagery
borrowed from that parse item's pages.js sidecar; all parses are
standalone job items — a direct ``extract -d`` records ``direct: true``
on its ref), or its ``markdown.md`` alone, opening on the Extract tab.

Every run re-scans ``$ADE_HOME/jobs/`` and rewrites ``history.js`` (the
sidebar's JSONP read model — manual deletions heal), then spawns a detached
background builder that fills in missing sibling viewers, flipping each
item's status ``none → building → built`` so sidebar links go live without
blocking this command.

Store scan/records/resolution live in ``items``; the sidebar read model in
``historyjs``; JOB_ITEM_ID gating in ``history`` (#58's layers) — this module
owns only the artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import typer

from . import attach, elements, evidence, historyjs, items, serve
from .config import ade_home
from .crop import DEFAULT_CROP_DPI, crop_element_to_file
from .history import resolve_or_exit
from .output import EXIT_FAILED, EXIT_USAGE, JSON_FLAG, emit, exit_with, tilde
from .store import JobStore
from .parse import _parse_pages
from .raster import CropError, render_source, source_drift_note

ARTIFACT = "view.html"
# Bump when the Python-side bundle shaping changes (the fields the template
# receives): the template hash alone can't see it, and a stale artifact
# would otherwise be reused forever (its item identity never moves).
BUNDLE_REVISION = 5
DEFAULT_DPI = 120
# Embed posture: modest dpi, first PAGE_CAP pages inline (--pages picks a
# different window); everything else lazy-loads from the sidecar chunks.
PAGE_CAP = 40
# view.html lives at jobs/<id>/view.html; history.js at the store root.
HISTORY_SRC = "../../history.js"


def _template() -> str:
    return (
        resources.files("ade_cli").joinpath("view_template.html").read_text("utf-8")
    )


def _load_parse(
    store: JobStore, item_id: str
) -> tuple[dict, dict, list[dict]] | None:
    """A generation-consistent parse as ``(meta, raw response, element
    records)`` — meta.json vouches only for the parse.json written with it,
    and elements.json serves only while its job_id stamp matches (else
    recomputed from raw; never written here)."""
    meta = store.read_json(item_id, "meta.json")
    if meta is None or meta.get("state") != "parsed":
        return None
    response = store.read_json(item_id, "parse.json")
    if (
        response is None
        or response.get("metadata", {}).get("job_id") != meta.get("job_id")
    ):
        return None
    stored = store.read_json(item_id, "elements.json")
    if stored is not None and stored.get("job_id") == meta.get("job_id"):
        records = stored["elements"]
    else:
        records = elements.project(response)
    return meta, response, records


def _extraction_payload(
    store: JobStore,
    extract_item_id: str,
    parse_meta: dict | None,
    parse_records: list[dict] | None,
) -> dict | None:
    """One extract item as a viewer layer, or None while it has no
    generation-consistent result. Stale (the referenced parse was --force
    re-parsed) keeps spans but never draws boxes — a stale layer must not
    place old-generation boxes on the live pages."""
    meta = store.read_json(extract_item_id, "meta.json") or {}
    job_id = meta.get("job_id")
    # A commit record without a server job id is not generation-consistent
    # (and None == None must never pass the gate below).
    if meta.get("state") != "extracted" or not isinstance(job_id, str):
        return None
    response = store.read_json(extract_item_id, "extract.json")
    if (
        response is None
        or response.get("metadata", {}).get("job_id") != job_id
    ):
        return None
    parse_job_id = (meta.get("params") or {}).get("parse_job_id")
    stale = (
        parse_meta is not None
        and parse_job_id is not None
        and parse_job_id != parse_meta.get("job_id")
    )
    stored_ev = store.read_json(extract_item_id, "evidence.json")
    if stored_ev is not None and stored_ev.get("job_id") == job_id:
        ev = stored_ev
    else:
        ev = evidence.build(
            response.get("extraction_metadata") or {},
            parse_records if (parse_records is not None and not stale) else None,
            job_id=job_id,
            parse_job_id=parse_job_id,
        )
    if stale and ev.get("kind") == "grounded":
        ev = {
            **ev,
            "kind": "spans_only",
            "reason": "parse_replaced",
            "fields": [
                {k: v for k, v in f.items()
                 if k not in ("element_ids", "pages", "boxes")}
                for f in ev.get("fields", [])
            ],
        }
    return {
        "job_item_id": extract_item_id,
        "job_id": job_id,
        "stale": stale,
        "model": response["metadata"].get("model_version")
        or response["metadata"].get("version"),
        # Billed credits ride in metadata.billing.total_credits on the wire;
        # metadata.credit_usage is the older field some stored responses
        # still carry (same precedence as historyjs._credits).
        "credit_usage": (response["metadata"].get("billing") or {}).get(
            "total_credits", response["metadata"].get("credit_usage")
        ),
        "schema": meta.get("schema"),
        "extraction": response.get("extraction"),
        "extraction_metadata": response.get("extraction_metadata"),
        # The partial-success signals (#118), verbatim from the response:
        # the Extract pane renders them as an advisory notice.
        "schema_violation_error": response.get("schema_violation_error"),
        "warnings": response.get("warnings") or [],
        "evidence": ev,
    }


def _referencing_extractions(
    store: JobStore, parse_item_id: str, parse_meta: dict, parse_records: list[dict]
) -> list[dict]:
    """Every extract item whose parse/ref.json points at this parse item —
    the parse viewer's layers, found in the same scan history.js derives
    from."""
    layers = []
    for item_id in items.referencing_extracts(store, parse_item_id):
        payload = _extraction_payload(store, item_id, parse_meta, parse_records)
        if payload is not None:
            layers.append(payload)
    return layers


def _imagery_source(store: JobStore, bundle: dict) -> str | None:
    """What this bundle's page imagery renders from: the parse item's
    recorded source, or its attached copy for URL parses (#169 —
    `parse --keep-copy` / `view --download`)."""
    owner = bundle.get("parse_item_id") or bundle["record"]["job_item_id"]
    return attach.renderable_source(store, owner, bundle["parse_meta"])


class BundleError(Exception):
    def __init__(self, kind: str, message: str, hint: str):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


def _load_bundle(store: JobStore, item_id: str) -> dict:
    """Everything the viewer needs for one job item: the parse pane's
    (meta, response, records), the extraction layers, and how the parse
    relates to the item (own | referenced | none)."""
    record = items.item_record(store, item_id)
    if record["kind"] == "parse":
        parse = _load_parse(store, item_id)
        if parse is None:
            state = record["state"]
            if state == "pending":
                hint = "a parse is pending; re-run `ade parse` to finish it"
            elif state == "unreadable":
                hint = (
                    "its parse job completed without a readable result; "
                    "re-run `ade parse` for the diagnosis"
                )
            else:
                hint = "run `ade parse` first"
            raise BundleError("not_parsed", f"no completed parse ({hint})", hint)
        meta, response, records = parse
        return {
            "kind": "parse",
            "record": record,
            "parse_meta": meta,
            "response": response,
            "records": records,
            # Parse viewers hold parse only: a document can carry many
            # extractions and the viewer can't guess which one the user
            # means — each extract item has its own viewer for that. Also
            # keeps parse artifacts stable when extractions come and go.
            "extractions": [],
            "linkage": "own",
        }

    # extract item: every shape owns its viewer (decision 8 as amended
    # 2026-07-21). A referencing extract builds a LIGHT artifact — the
    # parse pane's data rides in, but page imagery is reused from the
    # referenced parse item's pages.js sidecar, never duplicated.
    ref = record.get("parse")  # {job_item_id, parse_job_id, missing?} | None
    if ref is not None and not ref.get("missing"):
        parse_item_id = ref["job_item_id"]
        parse = _load_parse(store, parse_item_id)
        if parse is None:
            raise BundleError(
                "no_parse_linkage",
                f"its referenced parse job item {parse_item_id} has no "
                "completed parse",
                "re-run `ade parse` on it first",
            )
        meta, response, records = parse
        layer = _extraction_payload(store, item_id, meta, records)
        if layer is None:
            raise BundleError(
                "not_extracted",
                "no completed extraction (re-run `ade extract` to finish it)",
                "re-run `ade extract` to finish it",
            )
        return {
            "kind": "extract",
            "record": record,
            "parse_meta": meta,
            "response": response,
            "records": records,
            "extractions": [layer],
            "linkage": "referenced",
            "parse_item_id": parse_item_id,
        }
    # Bring-your-own-markdown item: no parse ever existed — the input
    # markdown was copied in as markdown.md (decision 9) and the viewer
    # renders the markdown pane alone, spans-only.
    md_path = store.item_dir(item_id) / "markdown.md"
    if ref is None and md_path.is_file():
        layer = _extraction_payload(store, item_id, None, None)
        if layer is None:
            raise BundleError(
                "not_extracted",
                "no completed extraction (re-run `ade extract` to finish it)",
                "re-run `ade extract` to finish it",
            )
        return {
            "kind": "markdown",
            "record": record,
            "parse_meta": store.read_json(item_id, "meta.json") or {},
            "response": None,
            "records": [],
            "markdown": md_path.read_text(encoding="utf-8"),
            "extractions": [layer],
            "linkage": "none",
        }
    if ref is not None and ref.get("missing"):
        hint = (
            f"its parse job item {ref['job_item_id']} was deleted "
            "(parse-missing); the stored extraction (extract.json, "
            "evidence.json) remains readable on disk"
        )
    else:
        hint = "it has no parse linkage (bring-your-own-markdown item)"
    raise BundleError(
        "no_parse_linkage", f"no renderable parse ({hint})", hint
    )


def _fingerprint(
    template: str,
    bundle: dict,
    *,
    dpi: int,
    pages: str | None,
    render_from: str | None,
) -> str:
    """What the artifact was built from: template, item kind/linkage, parse
    generation, the extraction layers (a new, newly-stale, or removed layer
    changes the artifact), build params, and the render source's
    identity-by-stat (a moved or edited source must rebuild — page images
    render from it; attaching a URL item's copy moves it too, #169)."""
    meta = bundle["parse_meta"]
    source = render_from
    path = Path(source) if source else None
    if path is not None and path.is_file():
        stat = path.stat()
        source_sig: object = [str(path), stat.st_size, stat.st_mtime_ns]
    else:
        source_sig = ["unavailable", source]
    payload = {
        "template": hashlib.sha256(template.encode()).hexdigest(),
        "bundle": BUNDLE_REVISION,
        "kind": bundle["kind"],
        "linkage": bundle["linkage"],
        "job_id": meta.get("job_id"),
        "extractions": [
            [ex["job_item_id"], ex["job_id"], ex["stale"]]
            for ex in bundle["extractions"]
        ],
        "dpi": dpi,
        "pages": pages,
        "cap": PAGE_CAP,
        "source": source_sig,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return digest[:16]


# Page-imagery sidecars: JSONP chunk files beside the artifacts (the same
# file:// trick as history.js), PAGES_CHUNK pages per file. view.html
# embeds only the first PAGE_CAP pages (the self-contained head that makes
# small documents one shareable file); every page loads on demand from
# these chunks when the viewer is opened from the store, so a 235-page
# document is fully browsable without a 60MB artifact. Each chunk file is
# independently fingerprinted: a partial render (interrupted background
# builder) heals on the next pass, chunk by chunk.
PAGES_CHUNK = 20


def _chunk_name(index: int) -> str:
    # Chunk 1 keeps the pages.js name light extract viewers always loaded.
    return "pages.js" if index == 1 else f"pages-{index}.js"


def _chunk_count(total_pages: int) -> int:
    return max(1, -(-total_pages // PAGES_CHUNK))


def _write_pages_chunk(
    store: JobStore,
    parse_item_id: str,
    index: int,
    total: int,
    images: dict,
    fingerprint: str,
) -> None:
    payload = json.dumps(
        {str(page): data for page, data in images.items()}, ensure_ascii=False
    ).replace("</", "<\\/")
    key = json.dumps(parse_item_id)
    # Per-item MERGE (never replace): several chunk files assign into the
    # same item key, in whatever order the viewer loads them.
    js = (
        f"// ade:pages fingerprint={fingerprint} chunk={index}/{total}\n"
        "window.__ADE_PAGES__ = window.__ADE_PAGES__ || {};\n"
        f"window.__ADE_PAGES__[{key}] = Object.assign("
        f"window.__ADE_PAGES__[{key}] || {{}}, {payload});\n"
    )
    store.write_text(parse_item_id, _chunk_name(index), js)


def _pages_fingerprint(render_from: str | None, total_pages: int) -> str:
    """What the sidecar chunks were rendered from: the render source's
    identity-by-stat and the render scheme. Imagery derives from the
    source alone (never the parse generation), so job_id stays out — a
    --force re-parse of an unchanged file must not re-render every page.
    ``render_from`` is the resolved local path (the recorded source, or a
    URL item's attached copy — #169)."""
    source = render_from
    path = Path(source) if source else None
    if path is not None and path.is_file():
        stat = path.stat()
        source_sig: object = [str(path), stat.st_size, stat.st_mtime_ns]
    else:
        source_sig = ["unavailable", source]
    payload = {
        "dpi": DEFAULT_DPI,
        "chunk": PAGES_CHUNK,
        "pages": total_pages,
        "source": source_sig,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]


def _chunk_current(
    store: JobStore, parse_item_id: str, index: int, fingerprint: str
) -> bool:
    path = store.item_dir(parse_item_id) / _chunk_name(index)
    try:
        with path.open(encoding="utf-8") as sidecar:
            first = sidecar.readline()
    except OSError:
        return False
    return first.startswith(f"// ade:pages fingerprint={fingerprint} chunk={index}/")


def _ensure_pages_chunks(
    store: JobStore,
    parse_item_id: str,
    meta: dict,
    all_pages: list[int],
    *,
    only: set[int] | None = None,
) -> int:
    """Render any missing/stale sidecar chunks (``only`` limits to those
    chunk indexes — the foreground path renders just the head; the
    background builder fills the rest). Returns chunks written. A source
    that yields no images stops the sweep — the viewer's placeholders and
    notice explain, and nothing half-written poses as current."""
    total = _chunk_count(len(all_pages))
    render_from = attach.renderable_source(store, parse_item_id, meta)
    fingerprint = _pages_fingerprint(render_from, len(all_pages))
    written = 0
    for index in range(1, total + 1):
        if only is not None and index not in only:
            continue
        if _chunk_current(store, parse_item_id, index, fingerprint):
            continue
        pages = all_pages[(index - 1) * PAGES_CHUNK : index * PAGES_CHUNK]
        images, _note = render_source(render_from, pages, dpi=DEFAULT_DPI, cap=0)
        if not images:
            break
        _write_pages_chunk(store, parse_item_id, index, total, images, fingerprint)
        written += 1
    return written


def _write_free_chunks(
    store: JobStore, item_id: str, meta: dict, all_pages: list[int], images: dict
) -> None:
    """Default-parameter parse builds already hold the head pages' exact
    images — write every chunk they fully cover for free; the background
    builder renders the rest."""
    total = _chunk_count(len(all_pages))
    render_from = attach.renderable_source(store, item_id, meta)
    fingerprint = _pages_fingerprint(render_from, len(all_pages))
    for index in range(1, total + 1):
        pages = all_pages[(index - 1) * PAGES_CHUNK : index * PAGES_CHUNK]
        chunk_images = {p: images[p] for p in pages if p in images}
        if len(chunk_images) != len(pages):
            break  # past the embed cap: not fully covered, not free
        _write_pages_chunk(store, item_id, index, total, chunk_images, fingerprint)


def _page_chunks_payload(
    all_pages: list[int], *, src_prefix: str
) -> list[dict]:
    """The viewer's lazy-load map: which sidecar file holds which pages."""
    total = _chunk_count(len(all_pages))
    return [
        {
            "src": src_prefix + _chunk_name(index),
            "pages": all_pages[(index - 1) * PAGES_CHUNK : index * PAGES_CHUNK],
        }
        for index in range(1, total + 1)
    ]


def _stored_fingerprint(path: Path) -> str | None:
    """The fingerprint comment is the artifact's first line — one cheap
    read decides reuse; an artifact without one (older CLI, torn write)
    simply rebuilds."""
    try:
        with path.open(encoding="utf-8") as artifact:
            first = artifact.readline()
    except OSError:
        return None
    marker = "ade:view fingerprint="
    start = first.find(marker)
    if start < 0:
        return None
    tokens = first[start + len(marker) :].split()
    return tokens[0] if tokens else None


def _element_json(response: dict, element_id: str) -> dict | None:
    """The element's own object from the raw response's structure tree —
    the parse.json slice the single-crop artifact shows."""
    def walk(node: dict):
        if node.get("id") == element_id:
            return node
        for child in node.get("children") or []:
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(response["structure"])


def _rows(records: list[dict], markdown: str) -> list[dict]:
    """The markdown pane's row list: one row per top-level element in
    document order, with the markdown between element spans kept as gap
    rows (page-break comments, the doc_id trailer)."""
    rows: list[dict] = []
    cursor = 0
    for index, record in enumerate(records):
        if record["type"] == "table_cell":
            continue  # rendered inside its table's row
        start, end = record["span"]
        if start > cursor and markdown[cursor:start].strip():
            rows.append({"gap": markdown[cursor:start]})
        rows.append({"el": index})
        cursor = max(cursor, end)
    if markdown[cursor:].strip():
        rows.append({"gap": markdown[cursor:]})
    return rows


def _build(
    store: JobStore,
    item_id: str,
    bundle: dict,
    *,
    template: str,
    dpi: int,
    pages_spec: str | None,
    fingerprint: str,
    built_at: str,
    render_from: str | None,
) -> tuple[Path, int, str | None]:
    """Stamp the template with this job item's data; returns
    ``(path, pages embedded, degradation note)``."""
    meta = bundle["parse_meta"]
    response = bundle["response"]
    records = bundle["records"]
    if bundle["kind"] == "markdown":
        # Bring-your-own markdown: no parse, so no page imagery and no
        # elements — the markdown pane renders the input whole; the JSON
        # tab shows the raw extract response (the item's ground truth).
        structure_pages: list[dict] = []
        all_pages: list[int] = []
        images: dict = {}
        note = "bring-your-own markdown — no page imagery to render"
        markdown_text = bundle["markdown"]
        raw_response = store.read_json(item_id, "extract.json") or {}
        response_meta = raw_response.get("metadata") or {}
    else:
        structure_pages = [
            {
                "page": page["grounding"]["page"],
                "status": page.get("status", "ok"),
            }
            for page in response["structure"]["children"]
        ]
        wanted = [p["page"] for p in structure_pages]
        if pages_spec is not None:
            selection = set(_parse_pages(pages_spec))
            wanted = [p for p in wanted if p in selection]
        all_pages = [p["page"] for p in structure_pages]
        if bundle["linkage"] == "referenced":
            # Light artifact: page imagery is not embedded — it rides in at
            # runtime from the referenced parse item's sidecar chunks
            # (the head chunk rendered here iff missing/stale; the rest by
            # the background builder), so imagery is stored once per
            # document however many extractions cite it. The chunks are
            # SHARED per parse item and always cover the full page list —
            # this build's --pages selection must not narrow them.
            _ensure_pages_chunks(
                store, bundle["parse_item_id"], meta, all_pages, only={1}
            )
            images, note = {}, None
        else:
            images, note = render_source(
                render_from, wanted, dpi=dpi, cap=PAGE_CAP
            )
            # A missing/URL source is a stable input (its reappearance
            # changes the fingerprint), but a render *error* is transient —
            # stamp a never-matching fingerprint so the next run retries.
            if note is not None and note.startswith("source unrenderable"):
                fingerprint = "retry-" + fingerprint
            if (
                bundle["kind"] == "parse"
                and dpi == DEFAULT_DPI
                and pages_spec is None
            ):
                # Default-parameter parse builds already hold the head
                # pages' exact images — write the chunks they cover free.
                _write_free_chunks(store, item_id, meta, all_pages, images)
        markdown_text = response["markdown"]
        raw_response = response
        response_meta = response["metadata"]
    # The lazy-load map: which sidecar file serves which page at runtime.
    # Empty when nothing can render (dead source, bring-your-own markdown)
    # — the placeholders then explain instead of spinning forever.
    source_dead = note is not None and note.startswith("source un")
    if bundle["kind"] == "markdown" or source_dead:
        page_chunks: list[dict] = []
        pages_key = None
    elif bundle["linkage"] == "referenced":
        owner = bundle["parse_item_id"]
        # Emit only when the head chunk actually exists — a source that
        # rendered nothing can't produce the rest either.
        if _chunk_current(
            store, owner, 1, _pages_fingerprint(render_from, len(all_pages))
        ):
            page_chunks = _page_chunks_payload(
                all_pages, src_prefix=f"../{owner}/"
            )
            pages_key = owner
        else:
            page_chunks, pages_key = [], None
            if render_from and render_from.startswith(("http://", "https://")):
                # A URL parse without an attached copy can never render
                # the sidecars this light artifact loads from — surface
                # the same cause + action banner the parse viewer gets
                # (#169) instead of bare placeholders.
                _, note = render_source(render_from, [], dpi=DEFAULT_DPI, cap=0)
    else:
        page_chunks = _page_chunks_payload(all_pages, src_prefix="")
        pages_key = item_id
    # What the artifact's warning banner shows: degradations only. Pages
    # that lazy-load are normal operation — their placeholders speak for
    # themselves — so that case informs the CLI summary but never the
    # banner (a 235-page doc must not open under a warning).
    banner_note = note
    # A URL item's missing preview gets the id-bearing action (#169): the
    # cause comes from the raster layer; the command that fixes it needs
    # the item id, which only this layer holds.
    if note and note.startswith("source unavailable: parsed from a URL"):
        owner_id = bundle.get("parse_item_id") or item_id
        action = (
            f" Fetch them with `ade view {items.short_id(store, owner_id)} "
            "--download`, or keep a copy at parse time with "
            "`parse --keep-copy`."
        )
        note += action
        banner_note = note
    # Renders from an attached copy carry the unverifiable-bytes caveat
    # (#169) — short and actionable in the CLI note; in the artifact it
    # renders as a compact banner line whose hover carries the full
    # story, like the stale badge.
    copy_caveat = attach.caveat(
        store, bundle.get("parse_item_id") or item_id, meta
    )
    show_caveat = bool(copy_caveat and (images or page_chunks))
    if show_caveat:
        note = copy_caveat if note is None else f"{note}; {copy_caveat}"
    if page_chunks and note and "not embedded (cap" in note:
        deferred_pages = [p for p in all_pages if p not in images]
        note = (
            f"{len(deferred_pages)} page(s) beyond the embedded head load "
            "on demand from the store"
        )
        banner_note = None
    # Content drift is a degradation the banner must carry (issue #119):
    # a changed source rebuilds (the fingerprint is stat-based) and the
    # fresh imagery no longer matches the parsed boxes and elements.
    # Markdown items render from the copied-in markdown, never the source
    # file, so drift cannot affect what this artifact shows.
    drift = source_drift_note(meta) if bundle["kind"] != "markdown" else None
    if drift:
        note = drift if note is None else f"{drift}; {note}"
        banner_note = drift if banner_note is None else f"{drift}; {banner_note}"
    source = meta.get("source")
    payload = {
        "receipt": {
            "job_item_id": item_id,
            "kind": bundle["kind"],
            "source": source,
            "source_name": (
                Path(source).name
                if source and not source.startswith(("http://", "https://"))
                else source
            ),
            "job_id": response_meta.get("job_id"),
            "model_version": response_meta.get("model_version"),
            "page_count": response_meta.get("page_count"),
            "failed_pages": response_meta.get("failed_pages") or [],
            "billing": response_meta.get("billing"),
            "built_at": built_at,
        },
        "job_item_id": item_id,
        "kind": bundle["kind"],
        "history_src": HISTORY_SRC,
        "page_chunks": page_chunks,
        "pages_key": pages_key,
        "note": banner_note,
        "pages": [
            {**page, **(images.get(page["page"]) or {"data_uri": None})}
            for page in structure_pages
        ],
        "elements": records,
        "rows": _rows(records, markdown_text),
        "markdown": markdown_text,
        "raw_response": raw_response,
        "extractions": bundle["extractions"],
        "copy_caveat": (
            {
                "text": "page previews render from a downloaded copy of the URL",
                "detail": attach.CAVEAT_DETAIL,
            }
            if show_caveat
            else None
        ),
    }
    for page in payload["pages"]:
        page["image"] = page.pop("data_uri")
    # "</" would close the carrier <script> tag from inside a JSON string;
    # "<\/" is the same JSON value and inert in HTML.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = template.replace("__FINGERPRINT__", fingerprint, 1).replace(
        "__ADE_DATA__", data, 1
    )
    return store.write_text(item_id, ARTIFACT, html), len(images), note


def ensure_built(
    store: JobStore,
    item_id: str,
    *,
    template: str,
    dpi: int = DEFAULT_DPI,
    pages: str | None = None,
    now: float,
) -> tuple[Path, bool, int | None, str | None]:
    """Build the item's viewer iff its fingerprint moved; the shared path
    behind the foreground command and the background builder. Raises
    BundleError when the item has nothing renderable."""
    bundle = _load_bundle(store, item_id)
    render_from = _imagery_source(store, bundle)
    fingerprint = _fingerprint(
        template, bundle, dpi=dpi, pages=pages, render_from=render_from
    )
    path = store.item_dir(item_id) / ARTIFACT
    if _stored_fingerprint(path) == fingerprint:
        return path, False, None, None
    built_at = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    path, embedded, note = _build(
        store,
        item_id,
        bundle,
        template=template,
        dpi=dpi,
        pages_spec=pages,
        fingerprint=fingerprint,
        built_at=built_at,
        render_from=render_from,
    )
    return path, True, embedded, note


def _refresh_history(store: JobStore, now: float) -> list[dict]:
    """Re-scan the store and rewrite history.js — every run, so manually
    deleted job items drop out of the sidebar."""
    records = items.item_records(store)
    historyjs.write(store, records, now=now)
    return records


def _builds_own_viewer(record: dict) -> bool:
    """Every job item owns its viewer (decision 8 as amended 2026-07-21 —
    referencing extracts build a light artifact over the parse's pages.js
    sidecar) except orphaned refs (parse manually deleted), which have
    nothing renderable."""
    ref = record.get("parse")
    return ref is None or not ref.get("missing")


def _latest_viewable(store: JobStore, records: list[dict]) -> str | None:
    """The default JOB_ITEM_ID for a bare ``view``: the newest-submitted item
    whose viewer can actually build. Pending/failed runs and orphaned
    refs are skipped by state; a generation-torn item (its bundle won't
    load) is skipped by trying — an older viewable sibling beats an
    error. ``records`` sorts timestamp-less husks last, so they are
    tried last here too, never ahead of a genuinely newest item."""
    dated = [r for r in records if r["submitted_at"] is not None]
    undated = [r for r in records if r["submitted_at"] is None]
    for record in [*reversed(dated), *undated]:
        if record["state"] not in ("parsed", "extracted"):
            continue
        if not _builds_own_viewer(record):
            continue
        try:
            _load_bundle(store, record["job_item_id"])
        except BundleError:
            continue
        return record["job_item_id"]
    return None


def _reexec_argv(*command: str) -> list[str]:
    """argv that re-runs this CLI: a PyInstaller binary (sys.frozen) takes
    subcommands directly; a normal install goes through ``python -m ade_cli``
    (the only re-exec an importable package can promise)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *command]
    return [sys.executable, "-m", "ade_cli", *command]


def _needs_chunks(store: JobStore, record: dict) -> bool:
    """Whether a parse item's page-imagery chunks are missing or stale —
    cheap (meta.json plus one first-line read per chunk file), so every
    view run can gate the background spawn on it."""
    if record["kind"] != "parse" or record["state"] != "parsed":
        return False
    pages = record.get("page_count") or 0
    if not pages:
        return False
    meta = store.read_json(record["job_item_id"], "meta.json") or {}
    source = attach.renderable_source(store, record["job_item_id"], meta) or ""
    if source.startswith(("http://", "https://")) or not Path(source).is_file():
        # Nothing local to render from (URL source without an attached
        # copy, or the file is gone): chunks can never materialize, and
        # answering True here would detach a futile builder on every
        # single view run.
        return False
    fingerprint = _pages_fingerprint(source, pages)
    return any(
        not _chunk_current(store, record["job_item_id"], index, fingerprint)
        for index in range(1, _chunk_count(pages) + 1)
    )


def _detach_kwargs() -> dict:
    """How a background child actually detaches, per platform. POSIX:
    ``start_new_session`` (its own session, immune to the parent's
    signals). Windows silently *ignores* ``start_new_session``, so the
    "detached" child used to stay attached to the parent console — a
    Ctrl-C or a closed terminal killed the builder mid-build. DETACHED
    (no console at all — stdio is DEVNULL anyway) plus a new process
    group makes the detachment real there."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        }
    return {"start_new_session": True}


def _spawn_builder(store: JobStore, records: list[dict]) -> bool:
    """Detach the background builder when any sibling viewer — or any
    parse item's page-imagery chunks — is missing. The child re-execs
    this CLI with the hidden --sync-viewers flag; its own history.js
    rewrites flip statuses none → building → built."""
    needs = any(
        r["state"] in ("parsed", "extracted")
        and _builds_own_viewer(r)
        and (
            historyjs.viewer_status(store, r["job_item_id"]) == "none"
            or _needs_chunks(store, r)
        )
        for r in records
    )
    if not needs:
        return False
    subprocess.Popen(
        _reexec_argv("view", "--sync-viewers"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **_detach_kwargs(),
    )
    return True


def _spawn_server(port: int) -> None:
    """Detach the viewer file server (serve.py's daemon body) the same way
    the sidebar builder detaches: a re-exec of this CLI with a hidden
    flag, fully disowned. The daemon settles the final port itself and
    records it in server.json; the caller watches for that."""
    subprocess.Popen(
        _reexec_argv("view", "--serve-daemon", str(port)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **_detach_kwargs(),
    )


def _sync_viewers(store: JobStore, now_fn) -> dict:
    """The background builder body: claim each buildable item (marker file
    with our pid — a dead claim is ignored by the scan and overwritten
    here), build, unclaim, rewriting history.js around every transition so
    open sidebars see building → built without a rebuild."""
    template = _template()
    built, skipped, failed = [], [], []
    chunks = 0
    for record in _refresh_history(store, now_fn()):
        item_id = record["job_item_id"]
        if record["state"] not in ("parsed", "extracted") or not _builds_own_viewer(
            record
        ):
            skipped.append(item_id)
            continue
        marker = store.item_dir(item_id) / historyjs.BUILDING_MARKER
        try:
            with store.lock(item_id):
                if historyjs.viewer_status(store, item_id) == "building":
                    skipped.append(item_id)  # another live builder owns it
                    continue
                marker.write_text(str(os.getpid()), encoding="utf-8")
            _refresh_history(store, now_fn())
            try:
                _, did_build, _, _ = ensure_built(
                    store, item_id, template=template, now=now_fn()
                )
                (built if did_build else skipped).append(item_id)
                if record["kind"] == "parse":
                    # Fill the item's page-imagery chunks — the lazy-load
                    # tail the foreground command never renders. Chunk
                    # files are independently fingerprinted, so a builder
                    # killed mid-sweep resumes where it stopped.
                    live = items.live_parse(store, item_id)
                    if live is not None:
                        p_meta, p_response = live
                        chunks += _ensure_pages_chunks(
                            store,
                            item_id,
                            p_meta,
                            [
                                c["grounding"]["page"]
                                for c in p_response["structure"]["children"]
                            ],
                        )
            finally:
                marker.unlink(missing_ok=True)
                _refresh_history(store, now_fn())
        except BundleError:
            marker.unlink(missing_ok=True)
            failed.append(item_id)
        except Exception:  # a broken item must not stall the fleet
            marker.unlink(missing_ok=True)
            failed.append(item_id)
    return {"built": built, "skipped": skipped, "failed": failed, "chunks": chunks}


def view(
    ctx: typer.Context,
    job_item: str | None = typer.Argument(
        None,
        help="Job item id or unambiguous prefix (default: the latest "
        "viewable job item).",
        metavar="JOB_ITEM_ID",
    ),
    element_id: str | None = typer.Option(
        None, "--element-id", help="Emit a deep link to this element."
    ),
    crop: bool = typer.Option(
        False,
        "--crop",
        help="With --element-id: render that element's PNG crop instead of "
        "the HTML artifact.",
    ),
    open_browser: bool | None = typer.Option(
        None,
        "--open/--no-open",
        help="Open the result in the browser. Default: open when stdout is "
        "a terminal; --json runs and piped output never auto-open.",
    ),
    dpi: int | None = typer.Option(
        None, "--dpi", min=1,
        help=f"Page render dpi (default {DEFAULT_DPI}); with --crop, the crop "
        f"dpi (default {DEFAULT_CROP_DPI}).",
    ),
    pages: str | None = typer.Option(
        None, "--pages", help="Pages to embed images for, 1-indexed, e.g. '1,3-5'."
    ),
    download: bool = typer.Option(
        False, "--download",
        help="URL-parsed items: fetch the document from its recorded URL "
        "into the job item and render page previews from that copy — "
        "the parse itself never gives the CLI the bytes (#169). Plain "
        "HTTP, no API credits; the copy is unverified against the "
        "parsed run. Also works on an extract item id (fetches into "
        "its referenced parse item).",
    ),
    no_sidebar_sync: bool = typer.Option(
        False, "--no-sidebar-sync",
        help="Skip the background build of missing sibling viewers.",
    ),
    serve_mode: bool = typer.Option(
        False, "--serve",
        help="Open via a local server (http://127.0.0.1) instead of file:// "
        "— browser zoom then covers every viewer natively. Starts the "
        "server if needed; reuses a running one. It retires itself after "
        "30 idle minutes, or immediately with --stop-server.",
    ),
    stop_server: bool = typer.Option(
        False, "--stop-server",
        help="Stop the local viewer server (started by --serve) and exit.",
    ),
    sync_viewers: bool = typer.Option(
        False, "--sync-viewers", hidden=True,
        help="Internal: run the background builder loop in this process.",
    ),
    serve_daemon: int | None = typer.Option(
        None, "--serve-daemon", hidden=True,
        help="Internal: run the viewer file server in this process, "
        "trying this port first.",
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Build a job item's self-contained grounded HTML viewer."""
    ports = ctx.obj
    home = ade_home()
    store = JobStore(home)

    # Humans get the browser by default: an interactive run (stdout is a
    # terminal, not --json) opens the artifact it built; agents and pipes
    # keep the path-only contract. --open/--no-open override either way.
    should_open = (
        open_browser
        if open_browser is not None
        else (not as_json and ports.stdout_is_tty())
    )

    if serve_daemon is not None:
        serve.run_daemon(home, serve_daemon)  # serves until stopped/idle
        return

    if stop_server:
        try:
            port = serve.stop_server(home, ports)
        except serve.ServeError as error:
            exit_with(
                {"error": "server_stop_failed", "message": str(error)},
                str(error),
                as_json=as_json,
                code=EXIT_FAILED,
            )
        if port is None:
            emit(
                {"status": "server_not_running", "port": None},
                "No viewer server is running for this store.",
                as_json=as_json,
            )
        else:
            emit(
                {"status": "server_stopped", "port": port},
                f"Viewer server on http://127.0.0.1:{port} stopped.",
                as_json=as_json,
            )
        return

    if sync_viewers:
        report = _sync_viewers(store, ports.clock.now)
        emit(
            {"status": "synced", **report},
            f"Built {len(report['built'])} viewer(s); "
            f"{len(report['skipped'])} up to date/skipped, "
            f"{len(report['failed'])} unbuildable, "
            f"{report['chunks']} page chunk(s) rendered.",
            as_json=as_json,
        )
        return

    # The always-heals contract: rewrite history.js from a fresh scan
    # before anything can exit early (unknown id, unbuildable item,
    # --crop) — a run that errors still heals the sidebar. Refreshed again
    # after a build so the current item's viewer status lands too.
    scanned = _refresh_history(store, ports.clock.now())

    if job_item is None:
        # Bare `view` targets the latest viewable item — the run the user
        # just did; an empty (or all-pending) store errors with
        # remediation. Reuses the scan that just rewrote history.js, so
        # the selection and the sidebar agree.
        item_id = _latest_viewable(store, scanned)
        if item_id is None:
            message = (
                "No viewable job items stored; run `ade parse` first, or "
                "pass a job item id (`ade history list` shows the store)."
            )
            exit_with(
                {"error": "no_viewable_items", "message": message},
                message,
                as_json=as_json,
                code=EXIT_FAILED,
            )
    else:
        item_id = resolve_or_exit(store, job_item, as_json=as_json)
    if pages is not None:
        try:
            _parse_pages(pages)
        except ValueError as err:
            exit_with(
                {"error": "bad_pages", "message": str(err)},
                str(err),
                as_json=as_json,
                code=EXIT_USAGE,
            )

    record = items.item_record(store, item_id)

    # --download (#169): attach the URL document's bytes to the imagery
    # owner (the item itself, or a referencing extract's parse item)
    # BEFORE the bundle loads, so this same run renders from the copy.
    download_line = ""
    downloaded: bool | None = None
    if download:
        if record["kind"] == "parse":
            owner_id = item_id
        else:
            ref = record.get("parse") or {}
            owner_id = (
                ref.get("job_item_id") if not ref.get("missing") else None
            )
        owner_meta = (
            store.read_json(owner_id, "meta.json") if owner_id else None
        )
        if not owner_id or not attach.is_url_source(owner_meta):
            message = (
                f"Job item {item_id} has no URL source to download: "
                "--download applies to items parsed from --document-url "
                "(local parses render previews from their file directly)."
            )
            exit_with(
                {
                    "error": "not_a_url_source",
                    "job_item_id": item_id,
                    "message": message,
                },
                message,
                as_json=as_json,
                code=EXIT_USAGE,
            )
        already = attach.attached_file(store, owner_id, owner_meta)
        if already is not None:
            downloaded = False
            download_line = (
                f"\n  download: copy already attached ({already.name}); "
                "previews render from it"
            )
        else:
            try:
                name, size = attach.download(
                    store,
                    owner_id,
                    owner_meta or {},
                    transport=ports.transport,
                    now=ports.clock.now(),
                )
            except attach.AttachError as error:
                exit_with(
                    {
                        "error": error.kind,
                        "job_item_id": owner_id,
                        "message": error.message,
                    },
                    error.message,
                    as_json=as_json,
                    code=EXIT_FAILED,
                )
            downloaded = True
            download_line = (
                f"\n  download: fetched {name} ({size:,} bytes) into job "
                f"item {owner_id}"
            )

    try:
        bundle = _load_bundle(store, item_id)
    except BundleError as error:
        exit_with(
            {"error": error.kind, "job_item_id": item_id, "message": error.message},
            f"Job item {item_id}: {error.message}.",
            as_json=as_json,
            code=EXIT_FAILED,
        )

    records = bundle["records"]
    if crop and element_id is None:
        exit_with(
            {"error": "missing_element_id",
             "message": "--crop needs --element-id to know what to crop."},
            "--crop needs --element-id to know what to crop.",
            as_json=as_json,
            code=EXIT_USAGE,
        )
    element = None
    if element_id is not None:
        element = next((r for r in records if r["id"] == element_id), None)
        if element is None:
            exit_with(
                {"error": "unknown_element", "job_item_id": item_id,
                 "element_id": element_id},
                f"Job item {item_id} has no element {element_id!r}.",
                as_json=as_json,
                code=EXIT_FAILED,
            )

    if crop:
        assert element is not None and element_id is not None
        crop_dpi = dpi if dpi is not None else DEFAULT_CROP_DPI
        try:
            # Same rule as the standalone `crop`: a referencing extract's
            # imagery renders from the parse item's recorded source.
            crop_path, width, height = crop_element_to_file(
                store, item_id, element, dpi=crop_dpi, output=None,
                source_item_id=bundle.get("parse_item_id") or item_id,
            )
        except CropError as error:
            message = error.message
            if "parsed from a URL" in message:
                owner_id = bundle.get("parse_item_id") or item_id
                message += (
                    f" Fetch it with `ade view {owner_id} --download`, "
                    "then re-run."
                )
                tail = ""
            else:
                tail = (
                    " (a crop is never served from stale imagery; restore "
                    "the source and re-run)"
                )
            exit_with(
                {"error": error.kind, "job_item_id": item_id,
                 "element_id": element_id, "message": message},
                f"Cannot crop {element_id}: {message}{tail}.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        import base64

        extractions = bundle["extractions"]
        if bundle["kind"] == "parse":
            # The crop artifact still cites extraction fields grounded on
            # this element — collected here only; the parse *viewer* stays
            # extraction-free.
            extractions = _referencing_extractions(
                store, item_id, bundle["parse_meta"], bundle["records"]
            )
        data_uri = "data:image/png;base64," + base64.b64encode(
            crop_path.read_bytes()
        ).decode("ascii")
        citing_fields = [
            {"job_item_id": ex["job_item_id"], "field": f["field"], "value": f["value"]}
            for ex in extractions
            if not ex["stale"] and (ex["evidence"] or {}).get("kind") == "grounded"
            for f in ex["evidence"]["fields"]
            if element_id in (f.get("element_ids") or [])
        ]
        meta = bundle["parse_meta"]
        # Same drift rule as the standalone `crop` (issue #119): a changed
        # source still renders, with the mismatch said out loud. URL items
        # have no drift check; a crop from their attached copy carries the
        # unverified-bytes caveat instead — mirroring standalone crop.
        drift = source_drift_note(meta) or attach.caveat(
            store, bundle.get("parse_item_id") or item_id, meta
        )
        payload = {
            "job_item_id": item_id,
            "source_name": Path(meta["source"]).name if meta.get("source") else item_id,
            "element_id": element_id,
            "type": element["type"],
            "page": element["page"],
            "box": element["box"],
            "text": element["text"],
            "element_json": _element_json(bundle["response"], element_id),
            "fields": citing_fields,
            "schemas": [
                {"job_item_id": ex["job_item_id"], "schema": ex["schema"]}
                for ex in extractions
                if ex["job_item_id"] in {f["job_item_id"] for f in citing_fields}
            ],
            "image": data_uri,
            "width": width,
            "height": height,
            "dpi": crop_dpi,
            "warning": drift,
        }
        template = (
            resources.files("ade_cli")
            .joinpath("crop_template.html")
            .read_text("utf-8")
        )
        data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        html_path = crop_path.with_suffix(".html")
        store.write_text(
            item_id, f"crops/{html_path.name}", template.replace("__ADE_DATA__", data, 1)
        )
        if should_open:
            # Injectable and headless-safe (ports.browser never raises).
            ports.browser(html_path.resolve().as_uri())
        emit(
            {"status": "cropped", "job_item_id": item_id, "element_id": element_id,
             "page": element["page"], "path": str(crop_path),
             "html": str(html_path), "fields": len(citing_fields),
             "width": width, "height": height,
             **({"warning": drift} if drift else {})},
            f"Crop of {element_id} (page {element['page']}) -> {html_path}"
            f"\n  png:    {crop_path} ({width}x{height} px @ {crop_dpi} dpi)"
            f"\n  fields: {len(citing_fields)} extraction field(s) cite this element"
            + (f"\n  warning: {drift}" if drift else ""),
            as_json=as_json,
        )
        return

    page_dpi = dpi if dpi is not None else DEFAULT_DPI
    template = _template()
    render_from = _imagery_source(store, bundle)
    fingerprint = _fingerprint(
        template, bundle, dpi=page_dpi, pages=pages, render_from=render_from
    )
    path = store.item_dir(item_id) / ARTIFACT
    built = _stored_fingerprint(path) != fingerprint
    embedded: int | None = None
    note = None
    if built:
        built_at = datetime.fromtimestamp(
            ports.clock.now(), tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        path, embedded, note = _build(
            store,
            item_id,
            bundle,
            template=template,
            dpi=page_dpi,
            pages_spec=pages,
            fingerprint=fingerprint,
            built_at=built_at,
            render_from=render_from,
        )

    # The sidebar contract: every run refreshes history.js from a fresh
    # scan, then backgrounds any missing sibling builds (never blocking
    # this command; statuses flip in history.js as the builder works).
    history_records = _refresh_history(store, ports.clock.now())
    syncing = False if no_sidebar_sync else _spawn_builder(store, history_records)

    # Two doors onto the same artifact (serve.py's module doc): --serve
    # opens the loopback origin — browser zoom then covers every viewer
    # natively — and degrades to the file:// door with an honest note
    # when the server can't come up (never a hard failure: the artifact
    # on disk is always complete).
    serve_error = None
    uri = path.resolve().as_uri()
    if serve_mode:
        try:
            port = serve.ensure_server(home, ports, spawn=_spawn_server)
            uri = serve.url_for(port, item_id)
        except serve.ServeError as error:
            serve_error = str(error)
    deep_link = f"{uri}#element={element_id}" if element_id else None
    target = deep_link or uri
    # ports.browser is injectable and returns whether a browser plausibly
    # launched — a headless box degrades to the hint lines, never a lie.
    opened = should_open and ports.browser(target)
    payload = {
        "status": "viewed",
        "job_item_id": item_id,
        "kind": record["kind"],
        "path": str(path),
        "built": built,
        "pages_embedded": embedded,
        "note": note,
        "url": uri if serve_mode and serve_error is None else None,
        "serve_error": serve_error,
        "deep_link": deep_link,
        "history_items": len(history_records),
        "sidebar_sync": syncing,
    }
    if downloaded is not None:
        payload["downloaded"] = downloaded
    hint = f"ade view {items.short_id(store, item_id)} --open" + (
        f" --element-id {element_id}" if element_id else ""
    )
    tail = (
        f"\n  opened in browser: {target}"
        if opened
        else (
            f"\n  open:  {hint}   (opens in your browser)"
            f"\n  link:  {target}   (cmd-click)"
        )
    )
    human = (
        f"Viewer for job item {item_id} ({record['kind']}) -> {tilde(path)} "
        + ("(built)" if built else "(up to date)")
        + download_line
        + (f"\n  note: {note}" if note else "")
        + (
            f"\n  serve: {serve_error} — falling back to file://"
            if serve_error
            else ""
        )
        + (
            "\n  sidebar: building missing sibling viewers in the background"
            if syncing
            else ""
        )
        + tail
    )
    emit(payload, human, as_json=as_json)
