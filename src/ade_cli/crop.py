"""``crop`` — render element regions from the source document as PNGs.

The crop output format for multimodal agents that need to *look at*
evidence mid-reasoning: element ids discovered via ``find`` (or cited in
extraction evidence) become image files here. Same fail-safe rule as the
viewer's page pane, but stricter: rendering weakens gracefully in ``view``,
while a crop with no source is an error — never a stale image (issue #10).
A source whose *bytes changed* since the parse still renders — the current
file is not stale imagery — but carries a drift warning (issue #119): the
boxes were computed from the original bytes and may no longer match.

Selection is ``find``'s, not a second dialect: ``--element-id`` names
elements outright, and ``--type``/``--page``/``--all`` run the shared
filter (``elements.select``) over the same projection ``find`` searches.
"Crop every figure" is therefore one command rather than a find-jq-xargs
bridge (F3).

Crop semantics mirror vision-agent-service's DPT-3 pipeline (see
``raster.crop_box`` for the file/line citations). Crops are derived
artifacts: recomputable from the source + parse.json, stored under the
job item's ``crops/`` unless ``-o`` says otherwise.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

import typer

from . import attach, elements, items, store
from .config import ade_home
from .history import require_job_id, resolve_or_exit
from .output import EXIT_FAILED, EXIT_USAGE, JSON_FLAG, emit, exit_with, tilde
from .raster import CropError, crop_box, source_drift_note

DEFAULT_CROP_DPI = 300  # crops are for close reading; render crisp


def crop_element_to_file(
    jobs: store.JobStore,
    item_id: str,
    record: dict,
    *,
    dpi: int,
    output: Path | None,
    source_item_id: str | None = None,
) -> tuple[Path, int, int]:
    """The shared crop pipeline (used by ``crop`` and ``view --crop``):
    render, crop, write PNG. Returns ``(path, width, height)``; raises
    ``raster.CropError`` with its machine-readable kind on any honest
    failure. ``source_item_id`` names the parse item whose recorded source
    renders the imagery when it isn't ``item_id`` itself (a referencing
    extract item); the PNG still lands under ``item_id``'s ``crops/``."""
    owner = source_item_id or item_id
    meta = jobs.read_json(owner, "meta.json") or {}
    # URL parses render from their attached copy when one exists (#169);
    # local parses keep the recorded source and its honest errors.
    image = crop_box(
        attach.renderable_source(jobs, owner, meta),
        record["page"], record["box"], dpi=dpi,
    )
    if output is None:
        output = jobs.item_dir(item_id) / "crops" / f"{record['id']}@{dpi}dpi.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG")
    return output, image.size[0], image.size[1]


def parse_backing_or_exit(
    jobs: store.JobStore,
    item_id: str,
    *,
    as_json: bool,
) -> str:
    """The parse item whose elements and source imagery back this item's
    crops: the item itself for parse items, the referenced parse (through
    ``parse/ref.json``) for parse-backed extract items. Markdown-only
    extract items and orphaned refs are honest errors — with no live parse
    linkage there is nothing a crop could render from."""
    record = items.item_record(jobs, item_id)
    if record["kind"] == "parse":
        return item_id
    ref = record.get("parse")
    if ref is None:
        message = (
            f"Job item {item_id} is a bring-your-own-markdown extraction; "
            "it has no parse (and no page imagery) to crop from."
        )
        exit_with(
            {"error": "no_parse_linkage", "job_item_id": item_id,
             "message": message},
            message,
            as_json=as_json,
            code=EXIT_FAILED,
        )
    if ref.get("missing"):
        message = (
            f"Job item {item_id} references parse job item "
            f"{ref.get('job_item_id')}, which was deleted; a crop is never "
            "served without its parse."
        )
        exit_with(
            {
                "error": "no_parse_linkage",
                "job_item_id": item_id,
                "parse_item_id": ref.get("job_item_id"),
                "message": message,
            },
            message,
            as_json=as_json,
            code=EXIT_FAILED,
        )
    return ref["job_item_id"]


def records_or_exit(
    jobs: store.JobStore, item_id: str, *, as_json: bool
) -> list[dict]:
    """The item's element projection, or the not-parsed error surface."""
    records = elements.live_elements(jobs, item_id)
    if records is None:
        state = items.item_record(jobs, item_id)["state"]
        hint = (
            "a parse is pending; re-run `ade parse` to finish it"
            if state == "pending"
            else "run `ade parse` first"
        )
        exit_with(
            {"error": "not_parsed", "job_item_id": item_id, "state": state},
            f"Job item {item_id} has no completed parse ({hint}).",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    return records


def find_element_or_exit(
    jobs: store.JobStore,
    item_id: str,
    element_id: str,
    *,
    as_json: bool,
    records: list[dict] | None = None,
) -> dict:
    """The element record behind an id, with the same not-parsed /
    unknown-element error surfaces ``view`` uses. Callers that already
    hold the projection (``view``, batch ``crop``) pass ``records`` to
    avoid a second live_elements pass."""
    if records is None:
        records = records_or_exit(jobs, item_id, as_json=as_json)
    for record in records:
        if record["id"] == element_id:
            return record
    exit_with(
        {"error": "unknown_element", "job_item_id": item_id, "element_id": element_id},
        f"Job item {item_id} has no element {element_id!r}; try `ade find`.",
        as_json=as_json,
        code=EXIT_FAILED,
    )


def crop(
    job_id_token: str | None = typer.Argument(
        None, metavar="[JOB_ITEM_ID]", help="Job item id or unambiguous prefix."
    ),
    element_ids: list[str] = typer.Option(
        [], "--element-id", help="Element to crop (ids from `ade find`); repeatable."
    ),
    element_type: str | None = typer.Option(
        None,
        "--type",
        help="Crop every element of this type (text, table, figure, ...) — "
        "`find`'s filter, applied here.",
    ),
    page: int | None = typer.Option(
        None, "--page", help="Restrict the selection to this 1-indexed page."
    ),
    all_elements: bool = typer.Option(
        False,
        "--all",
        help="Crop every element the filters select (on its own: the whole "
        "item).",
    ),
    output: Path | None = typer.Option(
        None,
        "-o",
        "--output",
        help="Where the crops land: a directory (a single crop lands "
        "inside it too), or — for a single crop only — the PNG path "
        "itself (default: the job item's crops/ dir).",
    ),
    dpi: int = typer.Option(DEFAULT_CROP_DPI, "--dpi", min=1, help="Render dpi."),
    open_image: bool = typer.Option(
        False, "--open", help="Open the crop (or the directory holding them)."
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Crop element regions from the source document into PNGs: one
    `--element-id`, or a filtered batch (`--type figure`, `--page`,
    `--all`) with the same filters `ade find` searches by."""
    jobs = store.JobStore(ade_home())
    item_id = resolve_or_exit(
        jobs, require_job_id(job_id_token, as_json=as_json), as_json=as_json
    )
    # A parse item backs itself; a parse-backed extract item resolves
    # through its ref — either way, elements and imagery come from the
    # parse while the crops land under the addressed item's crops/.
    parse_item_id = parse_backing_or_exit(jobs, item_id, as_json=as_json)
    filtered = element_type is not None or page is not None or all_elements
    if not element_ids and not filtered:
        message = (
            "Provide --element-id, or a filter to crop in batch (--type / "
            "--page / --all). Discover ids with `ade find "
            f"{parse_item_id}`."
        )
        exit_with(
            {"error": "missing_element_id", "message": message},
            message,
            as_json=as_json,
            code=EXIT_USAGE,
        )
    # One crop addressed by id keeps the flat single-crop payload; any
    # filter (or several ids) is a batch, whose result is the list.
    batch = filtered or len(element_ids) > 1
    records = records_or_exit(jobs, parse_item_id, as_json=as_json)
    # An id that names nothing is an error even inside a batch — the
    # caller asserted it exists. A *filter* matching nothing is just an
    # empty set, exactly as it is in `find`.
    for named in element_ids:
        find_element_or_exit(
            jobs, parse_item_id, named, as_json=as_json, records=records
        )
    selected = elements.select(
        records, element_type=element_type, page=page, element_ids=element_ids
    )
    # One drift check per invocation, not per element: the batch renders
    # from a single recorded source, and hashing it once is the whole cost.
    # URL items have no drift check (no recorded content hash); a render
    # from their attached copy carries the unverified-bytes caveat instead.
    parse_meta = jobs.read_json(parse_item_id, "meta.json")
    drift = source_drift_note(parse_meta) or attach.caveat(
        jobs, parse_item_id, parse_meta
    )
    directory, single_file = _crop_target(
        jobs, item_id, output, dpi=dpi, batch=batch, as_json=as_json
    )

    crops: list[dict] = []
    for record in selected:
        try:
            path, width, height = crop_element_to_file(
                jobs,
                item_id,
                record,
                dpi=dpi,
                output=(
                    single_file
                    if single_file is not None
                    else directory / f"{record['id']}@{dpi}dpi.png"
                ),
                source_item_id=parse_item_id,
            )
        except CropError as error:
            # Source-level failures (missing file, missing page) are the
            # whole batch's failure, not one element's: stopping is the
            # never-a-stale-image rule, and a partial batch that quietly
            # skipped pages would read as a complete one. URL items get
            # the id-bearing fetch action instead of "restore the source"
            # (#169 — there was never a local file to restore).
            message = error.message
            if "parsed from a URL" in message:
                message += (
                    f" Fetch it with `ade view {parse_item_id} --download`, "
                    "then re-run this crop."
                )
                tail = ""
            else:
                tail = (
                    " (a crop is never served from stale imagery; restore "
                    "the source and re-run)"
                )
            exit_with(
                {"error": error.kind, "job_item_id": item_id,
                 "element_id": record["id"], "message": message},
                f"Cannot crop {record['id']}: {message}{tail}.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        crops.append(
            {
                "element_id": record["id"],
                "type": record["type"],
                "page": record["page"],
                "box": record["box"],
                "dpi": dpi,
                "path": str(path),
                "width": width,
                "height": height,
            }
        )

    # One payload shape for every crop run (#157): count + directory +
    # crops[], whether one element matched or many — a consumer never
    # branches on how many elements a filter happened to select.
    landed = (
        Path(crops[0]["path"]).parent if single_file is not None else directory
    )
    payload = {
        "status": "cropped",
        "job_item_id": item_id,
        "count": len(crops),
        "directory": str(landed),
        "crops": crops,
        **({"warning": drift} if drift else {}),
    }
    if not batch:
        single = crops[0]
        if open_image:
            webbrowser.open(Path(single["path"]).resolve().as_uri())
        emit(
            payload,
            (
                f"Cropped {single['element_id']} ({single['type']}, page "
                f"{single['page']}) -> {single['path']}"
                f"\n  {single['width']}x{single['height']} px @ {dpi} dpi"
                + (f"\n  warning: {drift}" if drift else "")
            ),
            as_json=as_json,
        )
        return

    if open_image and crops:
        # One window, not N: the directory the batch landed in.
        webbrowser.open(landed.resolve().as_uri())
    lines = [
        f"  {crop['element_id']:<14}  {crop['type']:<10}  p{crop['page']}  "
        f"{crop['width']}x{crop['height']} px  -> {Path(crop['path']).name}"
        for crop in crops
    ]
    header = (
        f"Cropped {len(crops)} element(s) from job item {item_id} @ {dpi} dpi "
        f"-> {tilde(landed)}/"
        if crops
        else "No elements matched; nothing cropped."
    )
    emit(
        payload,
        "\n".join(
            [header, *lines]
            + ([f"  warning: {drift}"] if drift and crops else [])
        ),
        as_json=as_json,
    )


def _crop_target(
    jobs: store.JobStore,
    item_id: str,
    output: Path | None,
    *,
    dpi: int,
    batch: bool,
    as_json: bool,
) -> tuple[Path, Path | None]:
    """Where the crops land, as ``(directory, single file or None)``: the
    item's ``crops/`` dir by default; ``-o`` naming a directory lands the
    crop(s) inside it in either mode (#156 — a directory target used to
    crash single crops with IsADirectoryError); a single crop's ``-o`` may
    name the PNG path itself. ``-o`` naming an existing *file* is a usage
    error for a batch rather than N crops overwriting each other."""
    if output is None:
        return jobs.item_dir(item_id) / "crops", None
    if output.is_dir():
        return output, None
    if not batch:
        # A single crop's -o names the PNG file to write.
        return output.parent, output
    if output.is_file():
        message = (
            f"-o {output} is a file; a batch crop writes several PNGs, so "
            "-o must name a directory."
        )
        exit_with(
            {"error": "output_not_a_directory", "path": str(output),
             "message": message},
            message,
            as_json=as_json,
            code=EXIT_USAGE,
        )
    # A batch -o that names nothing yet is a directory to create.
    return output, None
