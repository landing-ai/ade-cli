"""``view`` — the self-contained HTML artifact, driven through the CLI seam.

Acceptance criteria: single-file artifact with no external requests; pane
sync driven by embedded element data; fail-safe degradation when the source
is moved/deleted/URL; fingerprint-gated rebuilds; header receipt from stored
metadata — now keyed on the job-item store (``jobs/<job-item-id>/``), plus
the history sidebar contract: history.js rewritten on every run, background
builder spawned for missing sibling viewers.

Job items are seeded directly onto disk (fast, and independent of the
network verbs' transport scripting); the real-command seams are covered by
test_parse.py / test_extract*.py.
"""

import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from parse_fixtures import rich_parse_response

from ade_cli.store import derive_id, local_identity

PARSE_PARAMS = {"model": "dpt-3-pro-latest", "pages": None, "tier": "priority"}
BASE_TIME = 1_750_000_000.0


def _identity_for(source, path=None):
    """The identity components a seed uses — real bytes when the file
    exists, deterministic stand-ins otherwise (only uniqueness matters)."""
    import hashlib
    if path is not None and path.is_file():
        return local_identity(path, path.read_bytes())
    return {
        "source_hash": hashlib.sha256(source.encode()).hexdigest(),
        "content_hash": hashlib.sha256(("content:" + source).encode()).hexdigest(),
    }


def seed_parse_item(cli, *, path=None, url=None, data=None, params=None):
    """Write a completed parse job item the way parse's finalize does: raw
    response verbatim, markdown, commit record (identity included), claim
    ticket."""
    data = data or rich_parse_response()
    params = params or PARSE_PARAMS
    source = str(path) if path is not None else url
    identity = _identity_for(source, path)
    item_id = derive_id("parse", "production", identity, params)
    item = cli.home / "jobs" / item_id
    item.mkdir(parents=True, exist_ok=True)
    meta = data["metadata"]
    job_id = meta["job_id"]
    (item / "parse.json").write_text(json.dumps(data))
    (item / "parse.md").write_text(data["markdown"])
    (item / "job.json").write_text(json.dumps(
        {"v": 1, "kind": "parse", "job_id": job_id, "state": "completed",
         "params": params, "source": source, "submitted_at": BASE_TIME}
    ))
    (item / "meta.json").write_text(json.dumps({
        "job_item_id": item_id, "kind": "parse", "source": source,
        "environment": "production",
        "identity": identity, "state": "parsed", "params": params,
        "job_id": job_id,
        "model_version": meta.get("model_version"),
        "page_count": meta.get("page_count"),
        "failed_pages": meta.get("failed_pages") or [],
        "completed_at": BASE_TIME + 60,
    }))
    return item_id


def seed_extract_item(
    cli, *, source, parse_item_id=None, parse_job_id=None, direct=False,
    extract_job_id="extract-0009", schema=None, extraction=None,
    extraction_metadata=None, evidence=None,
):
    """Write a completed top-level extract job item: parse/ref.json records
    the parse linkage (decision 8 — extractions never nest, references
    never copy; ``direct: true`` marks a parse the extract run created)."""
    schema = schema or {"type": "object", "properties": {"total": {"type": "string"}}}
    params = {"schema_sha256": "abc", "model": "extract-latest",
              "options": {}, "parse_job_id": parse_job_id}
    id_params = {"schema": schema, "model": "extract-latest", "options": {}}
    if parse_item_id is not None:
        # Mirrors production (extract.py): parse variants of one document
        # share source/content hashes, so the parse linkage is part of the
        # extraction's identity — without it their extractions would
        # collide on one id.
        id_params["parse_job_item_id"] = parse_item_id
    item_id = derive_id("extract", "production", _identity_for(source), id_params)
    item = cli.home / "jobs" / item_id
    item.mkdir(parents=True, exist_ok=True)
    if parse_item_id is not None:
        ref = {"job_item_id": parse_item_id, "parse_job_id": parse_job_id}
        if direct:
            ref["direct"] = True
        (item / "parse").mkdir(exist_ok=True)
        (item / "parse" / "ref.json").write_text(json.dumps(ref))
    (item / "extract.json").write_text(json.dumps({
        "extraction": extraction or {"total": "€42"},
        "extraction_metadata": extraction_metadata
        or {"total": {"value": "€42", "ranges": None}},
        "metadata": {"job_id": extract_job_id,
                     "model_version": "extract-20260710", "credit_usage": 0.5},
    }))
    if evidence is not None:
        (item / "evidence.json").write_text(json.dumps(evidence))
    (item / "job.json").write_text(json.dumps(
        {"v": 1, "kind": "extract", "job_id": extract_job_id,
         "state": "completed", "params": params, "source": source,
         "submitted_at": BASE_TIME + 120}
    ))
    (item / "meta.json").write_text(json.dumps({
        "job_item_id": item_id, "kind": "extract", "source": source,
        "identity": _identity_for(source), "state": "extracted",
        "params": params, "schema": schema, "job_id": extract_job_id,
        "version": "extract-20260710", "completed_at": BASE_TIME + 180,
    }))
    return item_id


def replace_parse_generation(cli, item_id, *, job_id):
    """Simulate a --force re-parse: same job item id, new server job_id —
    the one mutation the immutable-variants model still allows in place."""
    item = cli.home / "jobs" / item_id
    data = rich_parse_response(job_id=job_id)
    (item / "parse.json").write_text(json.dumps(data))
    (item / "parse.md").write_text(data["markdown"])
    meta = json.loads((item / "meta.json").read_text())
    meta["job_id"] = job_id
    (item / "meta.json").write_text(json.dumps(meta))


def view_json(cli, *args, exit_code=0, sync=False):
    extra = () if sync else ("--no-sidebar-sync",)
    result = cli.invoke("view", *args, *extra, "--json")
    assert result.exit_code == exit_code, result.stdout
    return json.loads(result.stdout)


@pytest.fixture
def pdf(tmp_path):
    """A real (blank) two-page PDF, so page rendering exercises pypdfium2."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(2):
        doc.new_page(612, 792)
    buffer = io.BytesIO()
    doc.save(buffer)
    path = tmp_path / "invoice.pdf"
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture
def parsed(cli, pdf):
    """The rich two-page fixture seeded as a parse job item."""
    return seed_parse_item(cli, path=pdf), pdf


def artifact(cli, item_id):
    return (cli.home / "jobs" / item_id / "view.html").read_text(encoding="utf-8")


def embedded_data(cli, item_id):
    html = artifact(cli, item_id)
    return json.loads(
        html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    )


def history_js(cli):
    js = (cli.home / "history.js").read_text(encoding="utf-8")
    prefix = "window.__ADE_HISTORY__ = "
    assert js.startswith(prefix)
    return json.loads(js[len(prefix):].rstrip().rstrip(";").replace("<\\/", "</"))


# --- the artifact ---


def test_view_builds_a_self_contained_artifact(cli, parsed):
    item_id, _ = parsed

    payload = view_json(cli, item_id)

    assert payload["built"] is True
    assert payload["kind"] == "parse"
    assert payload["pages_embedded"] == 2
    html = artifact(cli, item_id)
    # Both page images embedded as data URIs; no external fetches anywhere.
    # (history.js rides in via a relative <script src> — the one deliberate
    # local include; the panes never depend on it.)
    assert html.count("data:image/webp;base64,") == 2
    for external in ('src="http', "href=\"http", "<link", "fetch("):
        assert external not in html
    for element_id in ("text-0", "table-0", "table_cell-3", "figure-0"):
        assert element_id in html


def test_view_header_receipt_comes_from_stored_metadata(cli, parsed):
    item_id, _ = parsed

    view_json(cli, item_id)

    data = embedded_data(cli, item_id)
    receipt = data["receipt"]
    assert receipt["job_item_id"] == item_id
    assert receipt["source_name"] == "invoice.pdf"  # header shows the file name
    assert receipt["job_id"] == "job-0001"
    assert receipt["model_version"] == "dpt-3-pro-20260710"
    assert receipt["page_count"] == 2
    assert receipt["billing"] == {"service_tier": "priority", "total_credits": 2.5}
    assert data["job_item_id"] == item_id
    assert data["history_src"] == "../../history.js"


def test_view_markdown_rows_tile_the_whole_markdown(cli, parsed):
    item_id, _ = parsed

    view_json(cli, item_id)

    data = embedded_data(cli, item_id)
    rows = data["rows"]
    element_rows = [r["el"] for r in rows if "el" in r]
    ids = [data["elements"][i]["id"] for i in element_rows]
    # Top-level document order; cells render inside their table's row.
    assert ids == ["text-0", "table-0", "text-1", "figure-0"]
    # The doc_id trailer is kept as a dim gap row, never silently dropped.
    assert any("doc_id=" in r.get("gap", "") for r in rows)


# --- rebuild rules ---


def test_view_is_a_noop_until_the_store_changes(cli, parsed):
    item_id, _ = parsed

    first = view_json(cli, item_id)
    again = view_json(cli, item_id)

    assert first["built"] is True and again["built"] is False

    # A --force re-parse (new generation, same job item) rebuilds.
    replace_parse_generation(cli, item_id, job_id="job-0002")
    assert view_json(cli, item_id)["built"] is True

    # Changed build params rebuild too.
    assert view_json(cli, item_id, "--dpi", "72")["built"] is True


def test_parse_viewer_holds_parse_only(cli, parsed):
    # A document can carry many extractions and the parse viewer can't
    # guess which one the user means — parse viewers embed NO extraction
    # layers (each extract item has its own viewer), and extractions coming
    # and going never rebuilds the parse artifact.
    item_id, pdf = parsed
    view_json(cli, item_id)

    parse_job = json.loads(
        (cli.home / "jobs" / item_id / "meta.json").read_text()
    )["job_id"]
    extract_id = seed_extract_item(
        cli, source=str(pdf), parse_item_id=item_id, parse_job_id=parse_job
    )

    payload = view_json(cli, item_id)

    assert payload["built"] is False  # a new extraction is not its concern
    assert embedded_data(cli, item_id)["extractions"] == []

    # The extraction renders in the extract item's own viewer instead.
    view_json(cli, extract_id)
    data = embedded_data(cli, extract_id)
    assert [ex["job_item_id"] for ex in data["extractions"]] == [extract_id]
    ev = data["extractions"][0]["evidence"]
    assert ev["kind"] == "grounded"
    assert ev["fields"][0] == {"field": "total", "value": "€42", "ungroundable": True}


def test_view_withholds_boxes_from_stale_extractions(cli, parsed):
    item_id, pdf = parsed
    parse_job = json.loads(
        (cli.home / "jobs" / item_id / "meta.json").read_text()
    )["job_id"]
    # Grounded against the current parse: stored evidence.json cites text-0.
    extract_id = seed_extract_item(
        cli, source=str(pdf), parse_item_id=item_id, parse_job_id=parse_job,
        extract_job_id="extract-0100",
        schema={"type": "object", "properties": {"who": {"type": "string"}}},
        extraction={"who": "Acme"},
        extraction_metadata={"who": {"value": "Acme", "ranges": [{"start": 0, "end": 3}]}},
        evidence={
            "job_id": "extract-0100", "parse_job_id": parse_job, "kind": "grounded",
            "fields": [{"field": "who", "value": "Acme", "spans": [[0, 3]],
                        "element_ids": ["text-0"], "pages": [1],
                        "boxes": [{"page": 1, "box": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}}]}],
        },
    )

    # --force re-parse: new generation, so the extraction goes stale — its
    # own viewer (parse viewers hold no extraction layers) must withhold
    # the old-generation boxes.
    replace_parse_generation(cli, item_id, job_id="job-reparse")

    view_json(cli, extract_id)
    ex = embedded_data(cli, extract_id)["extractions"][0]
    assert ex["stale"] is True
    # Its stored evidence was grounded, but a stale layer must never draw
    # boxes against the new parse: withheld to spans_only, boxes stripped.
    assert ex["evidence"]["kind"] == "spans_only"
    assert ex["evidence"]["reason"] == "parse_replaced"
    assert "element_ids" not in ex["evidence"]["fields"][0]
    assert "boxes" not in ex["evidence"]["fields"][0]
    assert ex["evidence"]["fields"][0]["spans"] == [[0, 3]]  # spans kept


def test_view_rebuilds_when_the_source_changes_on_disk(cli, parsed):
    item_id, pdf = parsed

    view_json(cli, item_id)
    pdf.touch()  # new mtime = new source signature

    payload = view_json(cli, item_id)
    assert payload["built"] is True
    # Same bytes: a rebuild alone is never drift (issue #119).
    assert payload["note"] is None


def test_view_of_a_changed_source_carries_a_drift_note(cli, parsed):
    """Issue #119: rewritten bytes rebuild the artifact, and both the CLI
    note and the artifact's warning banner say the imagery no longer
    matches the parsed elements."""
    import pypdfium2 as pdfium

    item_id, pdf = parsed
    view_json(cli, item_id)
    doc = pdfium.PdfDocument.new()
    for _ in range(3):
        doc.new_page(612, 792)
    buffer = io.BytesIO()
    doc.save(buffer)
    pdf.write_bytes(buffer.getvalue())

    payload = view_json(cli, item_id)

    assert payload["built"] is True
    assert "changed after" in payload["note"]
    assert "changed after" in embedded_data(cli, item_id)["note"]


def test_view_extraction_layer_carries_the_partial_signals(cli, parsed):
    """Issue #118: the Extract pane's layer payload carries
    schema_violation_error and warnings verbatim, so the viewer can render
    its amber advisory."""
    item_id, pdf = parsed
    parse_job = json.loads(
        (cli.home / "jobs" / item_id / "meta.json").read_text()
    )["job_id"]
    extract_id = seed_extract_item(
        cli, source=str(pdf), parse_item_id=item_id, parse_job_id=parse_job
    )
    ex_path = cli.home / "jobs" / extract_id / "extract.json"
    ex = json.loads(ex_path.read_text())
    ex["schema_violation_error"] = "'total' is a required property."
    ex["warnings"] = [{"message": "total could not be extracted"}]
    ex_path.write_text(json.dumps(ex))

    view_json(cli, extract_id)

    (layer,) = embedded_data(cli, extract_id)["extractions"]
    assert layer["schema_violation_error"] == "'total' is a required property."
    assert layer["warnings"] == [{"message": "total could not be extracted"}]


# --- extract job items ---


def test_view_of_a_referencing_extract_builds_a_light_viewer(cli, parsed):
    # Decision 8 as amended: a referencing extract mints its OWN view.html
    # (URL identity per job item) — a light artifact with the parse pane's
    # data inline but no page imagery, which rides in at runtime from the
    # referenced parse item's pages.js sidecar.
    item_id, pdf = parsed
    parse_job = json.loads(
        (cli.home / "jobs" / item_id / "meta.json").read_text()
    )["job_id"]
    extract_id = seed_extract_item(
        cli, source=str(pdf), parse_item_id=item_id, parse_job_id=parse_job
    )

    payload = view_json(cli, extract_id)

    assert payload["kind"] == "extract"
    assert "resolved_to" not in payload
    assert payload["path"].endswith(f"jobs/{extract_id}/view.html")
    data = embedded_data(cli, extract_id)
    assert data["receipt"]["job_id"] == parse_job  # parse pane data inline
    assert [ex["job_item_id"] for ex in data["extractions"]] == [extract_id]
    # No embedded imagery; the sidecar reference points at the parse item.
    assert all(p["image"] is None for p in data["pages"])
    # The whole two-page doc fits one chunk; src points at the parse item.
    assert data["page_chunks"] == [
        {"src": f"../{item_id}/pages.js", "pages": [1, 2]}
    ]
    assert data["pages_key"] == item_id
    html = artifact(cli, extract_id)
    assert "data:image/webp;base64," not in html  # light by construction
    # When the borrow fails at runtime, the artifact explains cause and
    # recovery (opened outside the store / parse item deleted; re-run
    # view; share the parse item's view.html for an image-bearing copy).
    assert "Page images unavailable" in html
    assert "share the parse job item's view.html instead" in html
    # The sidecar itself was rendered into the parse item, JSONP-shaped.
    sidecar = (cli.home / "jobs" / item_id / "pages.js").read_text()
    assert sidecar.splitlines()[0].startswith("// ade:pages fingerprint=")
    assert "window.__ADE_PAGES__" in sidecar
    assert sidecar.count("data:image/webp;base64,") == 2  # both pages, once


def test_pages_sidecar_written_free_by_default_parse_builds(cli, parsed):
    item_id, _ = parsed

    view_json(cli, item_id)  # default dpi, no --pages

    sidecar = cli.home / "jobs" / item_id / "pages.js"
    assert sidecar.is_file()
    first = sidecar.read_text().splitlines()[0]
    assert first.startswith("// ade:pages fingerprint=")


def test_pages_beyond_the_embed_cap_lazy_load_from_chunks(cli, parsed, monkeypatch):
    # Shrink the knobs so the two-page fixture exercises the big-document
    # path: one embedded page, one-page chunks.
    monkeypatch.setattr("ade_cli.view.PAGE_CAP", 1)
    monkeypatch.setattr("ade_cli.view.PAGES_CHUNK", 1)
    item_id, _ = parsed

    payload = view_json(cli, item_id)

    assert payload["pages_embedded"] == 1
    # The old wording read as failure (a 235-page doc showed "cap 40");
    # beyond-cap pages now lazy-load and the note says so.
    assert "load on demand" in payload["note"]
    assert "cap" not in payload["note"]
    data = embedded_data(cli, item_id)
    # The CLI summary carries the info note; the artifact's warning banner
    # must NOT — lazy loading is normal operation, and a big document must
    # not open under a warning.
    assert data["note"] is None
    assert data["pages_key"] == item_id
    assert data["page_chunks"] == [
        {"src": "pages.js", "pages": [1]},
        {"src": "pages-2.js", "pages": [2]},
    ]
    # Chunk 1 was written free from the embedded image; chunk 2 waits for
    # the background builder.
    item_dir = cli.home / "jobs" / item_id
    assert (item_dir / "pages.js").is_file()
    assert not (item_dir / "pages-2.js").is_file()

    report = json.loads(cli.invoke("view", "--sync-viewers", "--json").stdout)

    assert report["chunks"] == 1
    chunk = (item_dir / "pages-2.js").read_text(encoding="utf-8")
    assert chunk.splitlines()[0].startswith("// ade:pages fingerprint=")
    # Chunks MERGE into the item's key (several files, any load order).
    assert "Object.assign(" in chunk and "window.__ADE_PAGES__" in chunk
    assert chunk.count("data:image/webp;base64,") == 1
    # Everything current: a second sweep renders nothing.
    again = json.loads(cli.invoke("view", "--sync-viewers", "--json").stdout)
    assert again["chunks"] == 0


def test_missing_chunks_spawn_the_background_builder(cli, parsed, monkeypatch):
    monkeypatch.setattr("ade_cli.view.PAGE_CAP", 1)
    monkeypatch.setattr("ade_cli.view.PAGES_CHUNK", 1)
    spawned = []
    monkeypatch.setattr(
        "ade_cli.view.subprocess.Popen",
        lambda argv, **kwargs: spawned.append(argv) or None,
    )
    item_id, _ = parsed

    # The viewer itself builds fine, but chunk 2 is missing — the spawn
    # gate must see it even with every sibling viewer built.
    payload = view_json(cli, item_id, sync=True)

    assert payload["sidebar_sync"] is True
    assert len(spawned) == 1


def test_direct_extract_references_a_standalone_parse_with_a_flag(cli, parsed):
    # A direct `extract -d` (no pre-existing parse) creates a NORMAL
    # standalone parse job item and references it like any other — the only
    # trace is `direct: true` on the ref (provenance, not ownership). The
    # parse shows in listings/sidebar like any parse; the reuse scan sees it.
    item_id, pdf = parsed
    parse_job = json.loads(
        (cli.home / "jobs" / item_id / "meta.json").read_text()
    )["job_id"]
    extract_id = seed_extract_item(
        cli, source=str(pdf), parse_item_id=item_id, parse_job_id=parse_job,
        direct=True,
    )

    payload = view_json(cli, extract_id)

    assert payload["kind"] == "extract"
    assert payload["built"] is True
    assert payload["path"].endswith(f"jobs/{extract_id}/view.html")
    data = embedded_data(cli, extract_id)
    assert [ex["job_item_id"] for ex in data["extractions"]] == [extract_id]
    # The flag surfaces in the read model (data-only for now) and the parse
    # item remains a first-class sidebar row.
    record = history_js(cli)
    by_id = {i["id"]: i for i in record["items"]}
    assert by_id[extract_id]["parent"] == item_id
    assert by_id[extract_id]["direct"] is True
    assert by_id[item_id]["kind"] == "parse"
    assert by_id[item_id]["viewer"] in ("none", "built")  # first-class row


def test_view_renders_a_markdown_only_extract_item(cli, tmp_path):
    # Bring-your-own-markdown (decision 9): markdown.md copied in, no
    # parse/ at all — the viewer renders the markdown pane alone,
    # spans-only, with an explicit no-page-imagery note.
    md = "# Notes\n\nTotal is €42 per the vendor quote.\n"
    extract_id = seed_extract_item(
        cli, source=str(tmp_path / "notes.md"),
        extraction_metadata={"total": {"value": "€42", "ranges": [{"start": 18, "end": 21}]}},
    )
    (cli.home / "jobs" / extract_id / "markdown.md").write_text(md)

    payload = view_json(cli, extract_id)

    assert payload["built"] is True
    assert "resolved_to" not in payload  # owns its viewer
    assert "bring-your-own markdown" in payload["note"]
    data = embedded_data(cli, extract_id)
    assert data["kind"] == "markdown"
    assert data["pages"] == [] and data["elements"] == []
    assert data["markdown"] == md
    ev = data["extractions"][0]["evidence"]
    assert ev["kind"] == "spans_only" and ev["reason"] == "markdown_doc"
    assert ev["fields"][0]["spans"] == [[18, 21]]


def test_view_reports_a_dangling_parse_reference(cli, parsed, tmp_path):
    item_id, pdf = parsed
    extract_id = seed_extract_item(
        cli, source=str(pdf), parse_item_id=item_id, parse_job_id="job-0001"
    )
    import shutil

    shutil.rmtree(cli.home / "jobs" / item_id)  # delete the referenced parse

    payload = view_json(cli, extract_id, exit_code=1)

    assert payload["error"] == "no_parse_linkage"
    assert item_id in payload["message"]  # names the deleted parse item
    assert "remains readable" in payload["message"]


# --- fail-safe source access ---


def test_view_degrades_when_the_source_is_gone(cli, parsed):
    item_id, pdf = parsed
    pdf.unlink()

    payload = view_json(cli, item_id)

    assert payload["built"] is True
    assert payload["pages_embedded"] == 0
    assert "source unavailable" in payload["note"]
    html = artifact(cli, item_id)
    assert "data:image/webp" not in html
    assert "source unavailable" in html
    # Stored artifacts still render: the element data is all there.
    assert "table_cell-3" in html
    # A dead source emits no lazy-load map — placeholders explain instead
    # of spinning through chunk retries that can never succeed.
    assert embedded_data(cli, item_id)["page_chunks"] == []


def test_view_never_reuses_an_unrenderable_build(cli, tmp_path):
    # Bytes that are neither PDF nor a decodable image: the render errors,
    # the build degrades — and must retry next run.
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"not a pdf, not an image")
    item_id = seed_parse_item(cli, path=path)

    first = view_json(cli, item_id)
    again = view_json(cli, item_id)

    assert "unrenderable" in first["note"]
    assert first["built"] is True and again["built"] is True


def test_view_degrades_for_url_documents(cli):
    item_id = seed_parse_item(cli, url="https://example.com/invoice.pdf")

    payload = view_json(cli, item_id)

    assert payload["built"] is True
    assert "URL" in payload["note"]


def test_view_renders_an_image_source_as_its_single_page(cli, tmp_path):
    from PIL import Image

    path = tmp_path / "scan.png"
    Image.new("RGB", (100, 140), "white").save(path)
    item_id = seed_parse_item(cli, path=path)

    payload = view_json(cli, item_id)

    # The fixture claims two pages; an image source can only ever raster
    # page 1 — the other page keeps its placeholder.
    assert payload["pages_embedded"] == 1


def test_view_pages_flag_limits_embedded_images(cli, parsed):
    item_id, _ = parsed

    payload = view_json(cli, item_id, "--pages", "1")

    assert payload["pages_embedded"] == 1


# --- deep links & errors ---


def test_view_emits_a_deep_link_for_a_known_element(cli, parsed):
    item_id, _ = parsed

    payload = view_json(cli, item_id, "--element-id", "table_cell-3")

    assert payload["deep_link"].startswith("file://")
    assert payload["deep_link"].endswith("view.html#element=table_cell-3")


def test_view_rejects_an_unknown_element_id(cli, parsed):
    item_id, _ = parsed

    payload = view_json(cli, item_id, "--element-id", "nope-9", exit_code=1)

    assert payload["error"] == "unknown_element"
    assert payload["job_item_id"] == item_id


def test_view_without_a_job_id_targets_the_latest_viewable_item(cli, parsed, pdf):
    parse_item, _ = parsed
    parse_job = json.loads(
        (cli.home / "jobs" / parse_item / "meta.json").read_text()
    )["job_id"]
    extract_item = seed_extract_item(
        cli, source=str(pdf), parse_item_id=parse_item, parse_job_id=parse_job
    )

    # The extract item was submitted later; a bare `view` opens it.
    assert view_json(cli)["job_item_id"] == extract_item

    # A newer but pending run is not viewable — the default skips it.
    pending = cli.home / "jobs" / "cccc000011112222"
    pending.mkdir(parents=True)
    (pending / "job.json").write_text(json.dumps(
        {"v": 1, "kind": "parse", "job_id": "job-0099", "state": "pending",
         "params": PARSE_PARAMS, "source": str(pdf),
         "submitted_at": BASE_TIME + 999}
    ))
    assert view_json(cli)["job_item_id"] == extract_item


def test_view_without_a_job_id_on_an_empty_store_errors_with_remediation(cli):
    payload = view_json(cli, exit_code=1)

    assert payload["error"] == "no_viewable_items"
    assert "ade parse" in payload["message"]
    assert "history list" in payload["message"]


def test_view_resolves_an_unambiguous_prefix_and_rejects_ambiguity(cli, parsed, pdf):
    item_id, _ = parsed

    assert view_json(cli, item_id[:8])["job_item_id"] == item_id

    # A sibling variant (different params ⇒ different id) can make a short
    # prefix ambiguous; the error lists the candidates.
    other = seed_parse_item(
        cli, path=pdf, params={**PARSE_PARAMS, "pages": "1"}
    )
    common = ""
    for a, b in zip(item_id, other):
        if a != b:
            break
        common += a
    if common:  # ids share a prefix only by hash luck; guard the assertion
        payload = view_json(cli, common, exit_code=2)
        assert payload["error"] == "ambiguous_id"
        assert set(payload["candidates"]) == {item_id, other}

    payload = view_json(cli, "ffffffffffffffff", exit_code=1)
    assert payload["error"] == "unknown_id"


def test_view_requires_a_completed_parse(cli, tmp_path):
    # A pending claim ticket only — the new-layout equivalent of
    # `parse --wait 0`; view must refuse with remediation, not crash.
    item = cli.home / "jobs" / "aaaa000011112222"
    item.mkdir(parents=True)
    (item / "job.json").write_text(json.dumps(
        {"v": 1, "job_id": "job-0009", "state": "pending",
         "params": PARSE_PARAMS, "source": str(tmp_path / "pending.pdf"),
         "submitted_at": BASE_TIME}
    ))

    payload = view_json(cli, "aaaa000011112222", exit_code=1)

    assert payload["error"] == "not_parsed"
    assert "pending" in payload["message"]


def test_view_distinguishes_unreadable_completion_from_pending(cli, tmp_path):
    item = cli.home / "jobs" / "bbbb000011112222"
    item.mkdir(parents=True)
    (item / "job.json").write_text(json.dumps(
        {"v": 1, "job_id": "job-0009", "state": "unreadable",
         "reason": "markdown is dict, not str",
         "params": PARSE_PARAMS, "source": str(tmp_path / "unreadable.pdf"),
         "submitted_at": BASE_TIME}
    ))

    payload = view_json(cli, "bbbb000011112222", exit_code=1)
    assert payload["error"] == "not_parsed"

    human = cli.invoke("view", "bbbb000011112222", "--no-sidebar-sync")
    assert human.exit_code == 1
    assert "finish it" not in human.stdout  # the old unbounded-loop hint
    assert "diagnosis" in human.stdout


# --- the history sidebar contract ---


def test_view_rewrites_history_js_from_a_fresh_scan(cli, parsed, pdf):
    item_id, _ = parsed

    view_json(cli, item_id)

    payload = history_js(cli)
    assert [i["id"] for i in payload["items"]] == [item_id]
    record = payload["items"][0]
    assert record["kind"] == "parse"
    assert record["state"] == "parsed"
    assert record["source_name"] == "invoice.pdf"
    assert record["model_version"] == "dpt-3-pro-20260710"
    assert record["credits"] == 2.5
    assert record["viewer"] == "built"
    assert record["href"] == f"jobs/{item_id}/view.html"

    # Manual deletion heals: a removed sibling drops out on the next run.
    other = seed_parse_item(cli, params={**PARSE_PARAMS, "pages": "1"}, path=pdf)
    view_json(cli, item_id)
    assert {i["id"] for i in history_js(cli)["items"]} == {item_id, other}
    import shutil

    shutil.rmtree(cli.home / "jobs" / other)
    view_json(cli, item_id)
    assert [i["id"] for i in history_js(cli)["items"]] == [item_id]


def test_history_js_neutralizes_script_closing_sequences(cli):
    # A source is store-controlled data that history.js *executes* —
    # "</script>" inside it must never close the include (decision:
    # history.js injection posture).
    item_id = seed_parse_item(cli, url="https://x.test/</script><img src=x>")

    view_json(cli, item_id)

    raw = (cli.home / "history.js").read_text(encoding="utf-8")
    assert "</script" not in raw
    assert history_js(cli)["items"][0]["id"] == item_id


def test_view_spawns_the_background_builder_for_missing_siblings(
    cli, parsed, pdf, monkeypatch
):
    item_id, _ = parsed
    seed_parse_item(cli, path=pdf, params={**PARSE_PARAMS, "pages": "1"})
    spawned = []
    monkeypatch.setattr(
        "ade_cli.view.subprocess.Popen",
        lambda argv, **kwargs: spawned.append(argv) or None,
    )

    payload = view_json(cli, item_id, sync=True)

    assert payload["sidebar_sync"] is True
    assert len(spawned) == 1
    assert spawned[0] == [sys.executable, "-m", "ade_cli", "view", "--sync-viewers"]

    # Everything built ⇒ nothing to spawn; --no-sidebar-sync always skips.
    result = cli.invoke("view", "--sync-viewers", "--json")
    assert result.exit_code == 0
    assert view_json(cli, item_id, sync=True)["sidebar_sync"] is False
    assert view_json(cli, item_id)["sidebar_sync"] is False


def test_view_spawns_the_frozen_binary_without_python_dash_m(
    cli, parsed, pdf, monkeypatch
):
    """A PyInstaller binary IS the interpreter — `-m ade_cli` would land in
    typer's argv. The builder re-exec must call the binary's subcommand
    directly when sys.frozen is set."""
    item_id, _ = parsed
    seed_parse_item(cli, path=pdf, params={**PARSE_PARAMS, "pages": "1"})
    spawned = []
    monkeypatch.setattr(
        "ade_cli.view.subprocess.Popen",
        lambda argv, **kwargs: spawned.append(argv) or None,
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    payload = view_json(cli, item_id, sync=True)

    assert payload["sidebar_sync"] is True
    assert spawned == [[sys.executable, "view", "--sync-viewers"]]


def test_sync_viewers_builds_every_completed_item(cli, parsed, pdf):
    item_id, _ = parsed
    other = seed_parse_item(cli, path=pdf, params={**PARSE_PARAMS, "pages": "1"})

    result = cli.invoke("view", "--sync-viewers", "--json")

    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert set(report["built"]) == {item_id, other}
    assert report["failed"] == []
    payload = history_js(cli)
    assert all(i["viewer"] == "built" for i in payload["items"])


def test_sidebar_markup_ships_in_the_artifact(cli, parsed):
    item_id, _ = parsed

    view_json(cli, item_id)

    html = artifact(cli, item_id)
    assert 'id="histbar"' in html
    assert 'id="hb-toggle"' in html  # the header collapse toggle (VUI IconSidebar)
    assert "__ADE_HISTORY__" in html  # the sidebar reads the JSONP global
    assert "formatDistanceToNow" in html or "timeAgo" in html
    # The grouped document-name dropdown behind the search chevron.
    assert 'id="hb-namedrop"' in html and 'id="hb-search-toggle"' in html
    # The toolbar model chip (Figma 130:357): family label in the markup
    # contract; the full dated version rides on the title at runtime.
    assert 'id="modelchip"' in html and 'id="modellabel"' in html


# --- output advertises --open (#36) ---


def labeled_line(stdout, label):
    return next(
        line for line in stdout.splitlines() if line.strip().startswith(label)
    )


def test_view_output_advertises_a_runnable_open_command(cli, parsed):
    item_id, _ = parsed

    result = cli.invoke("view", item_id, "--no-sidebar-sync")

    assert result.exit_code == 0
    open_line = labeled_line(result.stdout, "open:")
    assert "ade view " in open_line and "--open" in open_line
    assert "opens in your browser" in open_line
    link_line = labeled_line(result.stdout, "link:")
    assert "file://" in link_line  # terminals linkify the URI
    assert "cmd-click" in link_line
    # The hinted short id is runnable: it resolves to the same item today.
    ref = open_line.split("ade view ", 1)[1].split()[0]
    assert view_json(cli, ref)["job_item_id"] == item_id


def test_view_element_hint_reproduces_the_deep_link(cli, parsed):
    item_id, _ = parsed

    result = cli.invoke("view", item_id, "--element-id", "text-0", "--no-sidebar-sync")

    assert result.exit_code == 0
    assert "#element=text-0" in labeled_line(result.stdout, "link:")
    assert "--open --element-id text-0" in labeled_line(result.stdout, "open:")


def test_view_open_reports_instead_of_hinting(cli, parsed):
    opened = []
    item_id, _ = parsed

    result = cli.invoke(
        "view", item_id, "--open", "--no-sidebar-sync",
        browser=lambda url: opened.append(url) or True,
    )

    assert result.exit_code == 0
    assert "opened in browser: file://" in result.stdout
    assert "cmd-click" not in result.stdout  # the hint is replaced, not stacked
    assert len(opened) == 1 and opened[0].startswith("file://")


def test_view_opens_the_browser_by_default_on_a_terminal(cli, parsed):
    opened = []
    fake = lambda url: opened.append(url) or True  # noqa: E731
    cli.stdout_tty = True
    item_id, _ = parsed

    result = cli.invoke("view", item_id, "--no-sidebar-sync", browser=fake)

    assert result.exit_code == 0
    assert "opened in browser: file://" in result.stdout
    assert len(opened) == 1

    # --no-open suppresses the terminal default; the hint line returns.
    # (The conftest default browser raises on any unscripted open.)
    opened.clear()
    result = cli.invoke("view", item_id, "--no-open", "--no-sidebar-sync")
    assert result.exit_code == 0
    assert opened == []
    assert "--open" in result.stdout


def test_view_falls_back_to_hints_when_no_browser_launches(cli, parsed):
    # Headless box: ports.browser reports failure — the output must not
    # claim "opened in browser"; the hint lines return instead.
    cli.stdout_tty = True
    item_id, _ = parsed

    result = cli.invoke(
        "view", item_id, "--no-sidebar-sync", browser=lambda url: False
    )

    assert result.exit_code == 0
    assert "opened in browser" not in result.stdout
    assert "--open" in result.stdout


def test_view_json_never_auto_opens(cli, parsed):
    # Agents keep the no-browser contract even on a terminal. The conftest
    # default browser raises on any unscripted open, so plain invocation
    # is itself the assertion.
    cli.stdout_tty = True
    item_id, _ = parsed

    payload = view_json(cli, item_id)

    assert payload["job_item_id"] == item_id


def test_view_json_payload_is_unchanged_by_the_hints(cli, parsed):
    payload = view_json(cli, parsed[0])

    assert set(payload) == {
        "status", "job_item_id", "kind", "path", "built", "pages_embedded",
        "note", "url", "serve_error", "deep_link", "history_items",
        "sidebar_sync",
    }


# --- schema panel: the template's JS is executed, not just string-matched -----
# A TypeError inside view.html is invisible to a Python assertion — the CLI still
# reports {"status": "viewed"} and the artifact still exists; only the rendered
# panel is truncated. These tests run the template's own functions under node so
# the failure mode that shipped (union types aborting schemaTree) has a guard.

SCHEMA_JS_HARNESS = """
%s

function fail(msg) { console.error(msg); process.exit(1); }

// A nullable leaf is the ordinary JSON Schema spelling: {"type": ["string", "null"]}.
const nullable = typeBadge({ type: ["string", "null"] });
if (nullable.label !== "String") fail("nullable leaf: " + nullable.label);
if (nullable.cls !== "t-string") fail("nullable class: " + nullable.cls);

const nullableNumber = typeBadge({ type: ["number", "null"] });
if (nullableNumber.label !== "Number") fail("nullable number: " + nullableNumber.label);

// Nullable array of nullable objects still reads as an array of its item type.
const nullableArray = typeBadge({ type: ["array", "null"], items: { type: ["object", "null"] } });
if (nullableArray.label !== "Array of Object") fail("nullable array: " + nullableArray.label);

// Plain (non-union) forms keep their existing labels.
if (typeBadge({ type: "object" }).label !== "Object") fail("object regressed");
if (typeBadge({ type: "string" }).label !== "String") fail("string regressed");
if (typeBadge({ type: "integer" }).label !== "Number") fail("integer regressed");
if (typeBadge({ type: "array", items: { type: "object" } }).label !== "Array of Object")
  fail("array regressed");
if (typeBadge({}).label !== "String") fail("missing type regressed");

// soleType keeps "null" when that is genuinely all there is.
if (soleType(["null"]) !== "null") fail("all-null union");
console.log("ok");
"""


def _type_badge_source():
    """The template's own soleType/typeBadge, lifted verbatim."""
    from ade_cli import view

    html = (pathlib.Path(view.__file__).parent / "view_template.html").read_text()
    fns = []
    for name in ("soleType", "typeBadge"):
        start = html.index("function %s(" % name)
        end = html.index("\n}\n", start) + len("\n}\n")
        fns.append(html[start:end])
    return "\n".join(fns)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_schema_badges_survive_nullable_union_types(tmp_path):
    # Regression: `["string","null"]` reached `norm.charAt(0)`, which arrays do not
    # have. The TypeError escaped schemaTree, so the schema panel stopped rendering
    # at the first nullable field — every field after it silently vanished.
    script = tmp_path / "badge.js"
    script.write_text(SCHEMA_JS_HARNESS % _type_badge_source())

    proc = subprocess.run(
        [shutil.which("node"), str(script)], capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_schema_tree_resolves_union_types_before_descending():
    # The sibling of the badge bug: a nullable array (`["array","null"]`) must still
    # descend into `items`, or its children never render.
    from ade_cli import view

    html = (pathlib.Path(view.__file__).parent / "view_template.html").read_text()

    assert 'const child = soleType(spec.type) === "array" ? spec.items : spec;' in html


def _crop_badge_source():
    """The crop sidebar's duplicated renderer (crop_template.html), lifted
    with its IIFE indentation removed; ``badge`` is aliased to the
    harness's ``typeBadge`` name so both templates face one contract."""
    from ade_cli import view

    html = (pathlib.Path(view.__file__).parent / "crop_template.html").read_text()
    fns = []
    for name in ("soleType", "badge"):
        match = re.search(
            r"^( *)function %s\(.*?^\1\}$" % name, html, re.S | re.M
        )
        assert match, name
        fns.append(textwrap.dedent(match.group(0)))
    return "\n".join(fns) + "\nconst typeBadge = badge;\n"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_crop_schema_badges_survive_nullable_union_types(tmp_path):
    # `ade view --crop` artifacts render schemas through their own copy of
    # the badge logic; without the same union normalization they keep the
    # truncation the viewer just fixed.
    script = tmp_path / "crop-badge.js"
    script.write_text(SCHEMA_JS_HARNESS % _crop_badge_source())

    proc = subprocess.run(
        [shutil.which("node"), str(script)], capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_crop_schema_tree_resolves_union_types_before_descending():
    from ade_cli import view

    html = (pathlib.Path(view.__file__).parent / "crop_template.html").read_text()

    assert 'const child = soleType(spec.type) === "array" ? spec.items : spec;' in html


def test_url_parsed_item_note_explains_the_missing_preview_and_the_fix(cli):
    """#169: a URL parse never hands the CLI the document bytes, so the
    viewer cannot render page previews — the note must say why, what
    still works, and the action that gets previews (not read as a broken
    viewer)."""
    item_id = seed_parse_item(cli, url="https://example.com/doc.pdf")

    payload = view_json(cli, item_id)

    assert payload["built"] is True
    assert payload["pages_embedded"] == 0
    assert "parsed from a URL" in payload["note"]
    assert "ade parse -d" in payload["note"]  # the remediation
    assert "unaffected" in payload["note"]  # what still works
    # The artifact carries the same story: banner note + placeholders
    # that point at it, and no lazy-load map that could spin forever.
    data = embedded_data(cli, item_id)
    assert "parsed from a URL" in data["note"]
    assert data["page_chunks"] == []
