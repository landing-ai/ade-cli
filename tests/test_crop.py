"""``crop`` — single-element PNG crops, driven through the CLI seam.

The issue #10 rules under test: the crop pipeline mirrors the
vision-agent-service pixel math (normalized box x raster dims, clamped),
requires the source present, and a missing source is a clear error —
never a stale image.
"""

import io
import json
import shutil
from pathlib import Path

import pytest

from extract_fixtures import SCHEMA, completed_extract_job, extract_result
from parse_fixtures import completed_job, rich_parse_response

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}


def parse_source(cli, path, *, job_id="job-0001", data=None):
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_job(data, job_id=job_id))
    result = cli.invoke("parse", "-d", str(path), "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


@pytest.fixture
def pdf(tmp_path):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(2):
        doc.new_page(612, 792)  # US letter, points
    buffer = io.BytesIO()
    doc.save(buffer)
    path = tmp_path / "invoice.pdf"
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture
def parsed(cli, pdf):
    item_id = parse_source(cli, data=rich_parse_response(), path=pdf)
    return item_id, pdf


@pytest.fixture
def schema_file(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(SCHEMA))
    return path


def extract_against(cli, parse_item_id, schema_file):
    """Seed a parse-backed extract item referencing ``parse_item_id``
    (parse/ref.json linkage) through the seam; returns its job item id."""
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200, completed_extract_job(extract_result(markdown=rich_parse_response()["markdown"]))
    )
    result = cli.invoke(
        "extract", parse_item_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


def crop_json(cli, *args, exit_code=0):
    result = cli.invoke("crop", *args, "--json")
    assert result.exit_code == exit_code, result.stdout
    return json.loads(result.stdout)


def test_crop_writes_a_png_with_service_pixel_math(cli, parsed):
    item_id, _ = parsed

    payload = crop_json(cli, item_id, "--element-id", "table_cell-0")

    # The default artifact lands in the doc's crops/ dir, dpi-stamped.
    (single,) = payload["crops"]
    assert single["path"].endswith("crops/table_cell-0@300dpi.png")
    from PIL import Image

    with Image.open(single["path"]) as image:
        assert image.format == "PNG"
        assert (image.width, image.height) == (single["width"], single["height"])
    # crop_region math: int(edge * raster) per side, page 612x792pt at
    # 300 dpi -> 2550x3300 px; the fixture cell's box is (.03,.06,.53,.56).
    assert single["width"] == int(0.53 * 2550) - int(0.03 * 2550)
    assert single["height"] == int(0.56 * 3300) - int(0.06 * 3300)
    assert single["page"] == 1


def test_crop_honors_output_path_and_dpi(cli, parsed, tmp_path):
    item_id, _ = parsed
    out = tmp_path / "cell.png"

    payload = crop_json(
        cli, item_id, "--element-id", "table_cell-0", "-o", str(out), "--dpi", "72"
    )

    (single,) = payload["crops"]
    assert single["path"] == str(out)
    assert payload["directory"] == str(out.parent)
    assert out.is_file()
    assert single["width"] == int(0.53 * 612) - int(0.03 * 612)  # 72 dpi = 1:1 pt


def test_single_crop_lands_inside_an_output_directory(cli, parsed, tmp_path):
    """A directory as -o is a landing spot in either mode (#156): the
    single crop takes the default dpi-stamped name inside it instead of
    crashing with IsADirectoryError."""
    item_id, _ = parsed
    out = tmp_path / "my_crops"
    out.mkdir()

    payload = crop_json(cli, item_id, "--element-id", "table_cell-0", "-o", str(out))

    (single,) = payload["crops"]
    assert payload["directory"] == str(out)
    assert single["path"] == str(out / "table_cell-0@300dpi.png")
    assert (out / "table_cell-0@300dpi.png").is_file()


def test_crop_of_a_missing_source_is_a_clear_error_never_stale(cli, parsed):
    item_id, pdf = parsed
    crop_json(cli, item_id, "--element-id", "text-0")  # a crop exists on disk
    pdf.unlink()

    payload = crop_json(cli, item_id, "--element-id", "text-0", exit_code=1)

    assert payload["error"] == "source_missing"
    assert "no longer exists" in payload["message"]


def test_crop_page_beyond_an_image_source_is_page_missing(cli, tmp_path):
    from PIL import Image

    path = tmp_path / "scan.png"
    Image.new("RGB", (100, 140), "white").save(path)
    item_id = parse_source(cli, data=rich_parse_response(), path=path)

    # figure-0 lives on page 2 of the fixture; an image source only has page 1.
    payload = crop_json(cli, item_id, "--element-id", "figure-0", exit_code=1)
    assert payload["error"] == "page_missing"

    # page-1 elements crop fine from the image itself.
    ok = crop_json(cli, item_id, "--element-id", "text-0")
    assert ok["crops"][0]["page"] == 1


def test_crop_requires_an_element_id(cli, parsed):
    item_id, _ = parsed

    payload = crop_json(cli, item_id, exit_code=2)

    assert payload["error"] == "missing_element_id"
    assert "ade find" in payload["message"]


def test_crop_rejects_an_unknown_element(cli, parsed):
    item_id, _ = parsed

    payload = crop_json(cli, item_id, "--element-id", "nope-9", exit_code=1)

    assert payload["error"] == "unknown_element"


# --- batch crop (F3): find's filters, applied on crop ----------------------


def test_crop_all_figures_is_one_command(cli, parsed):
    """The single most common reason to crop — pull the figures — must not
    need a find | jq | xargs bridge."""
    item_id, _ = parsed

    payload = crop_json(cli, item_id, "--type", "figure")

    assert payload["count"] == 1
    (crop,) = payload["crops"]
    assert crop["element_id"] == "figure-0"
    assert crop["type"] == "figure"
    assert Path(crop["path"]).is_file()
    assert payload["directory"] == str(cli.home / "jobs" / item_id / "crops")


def test_batch_crop_selects_exactly_what_find_selects(cli, parsed):
    """One filter implementation, not two agreeing: whatever `find` returns
    for a filter is what `crop` renders for it."""
    item_id, _ = parsed
    for filters in (["--all"], ["--type", "table_cell"], ["--page", "1"]):
        found = cli.invoke("find", item_id, *filters_for_find(filters), "--json")
        expected = [m["element_id"] for m in json.loads(found.stdout)]

        payload = crop_json(cli, item_id, *filters)

        assert [c["element_id"] for c in payload["crops"]] == expected
        assert payload["count"] == len(expected)


def filters_for_find(filters):
    """`--all` is crop's way of saying "no filter"; find already means that
    when given none."""
    return [f for f in filters if f != "--all"]


def test_batch_crop_filters_compose_and_write_every_png(cli, parsed):
    item_id, _ = parsed

    payload = crop_json(cli, item_id, "--type", "table_cell", "--page", "1", "--dpi", "72")

    assert payload["count"] == 4  # the fixture's four cells, all on page 1
    for crop in payload["crops"]:
        assert crop["page"] == 1
        assert crop["dpi"] == 72
        assert Path(crop["path"]).name == f"{crop['element_id']}@72dpi.png"
        assert Path(crop["path"]).is_file()


def test_batch_crop_honors_an_output_directory(cli, parsed, tmp_path):
    item_id, _ = parsed
    out = tmp_path / "figures"

    payload = crop_json(cli, item_id, "--type", "figure", "-o", str(out))

    assert payload["directory"] == str(out)
    assert (out / "figure-0@300dpi.png").is_file()


def test_batch_crop_rejects_a_file_as_the_output_target(cli, parsed, tmp_path):
    item_id, _ = parsed
    out = tmp_path / "one.png"
    out.write_bytes(b"")

    payload = crop_json(cli, item_id, "--all", "-o", str(out), exit_code=2)

    # Several PNGs cannot share one path; say so instead of overwriting.
    assert payload["error"] == "output_not_a_directory"


def test_a_filter_matching_nothing_is_an_empty_batch_not_an_error(cli, parsed):
    """Same posture as `find`: an empty selection is an empty result. Only
    an *id* that names nothing is an error — the caller asserted it."""
    item_id, _ = parsed

    payload = crop_json(cli, item_id, "--type", "no_such_type")

    assert payload["count"] == 0
    assert payload["crops"] == []

    unknown = crop_json(cli, item_id, "--element-id", "nope-9", exit_code=1)
    assert unknown["error"] == "unknown_element"


def test_several_element_ids_crop_as_a_batch(cli, parsed):
    item_id, _ = parsed

    payload = crop_json(
        cli, item_id, "--element-id", "text-0", "--element-id", "figure-0"
    )

    assert [c["element_id"] for c in payload["crops"]] == ["text-0", "figure-0"]
    # ...and an unknown id inside the batch is still the unknown_element
    # error, not a silently shorter list.
    partial = crop_json(
        cli, item_id, "--element-id", "text-0", "--element-id", "nope-9", exit_code=1
    )
    assert partial["error"] == "unknown_element"


def test_one_element_id_yields_the_same_shape_as_a_batch(cli, parsed):
    """One payload shape whatever matched (#157): a consumer never branches
    on how many elements a run happened to crop."""
    item_id, _ = parsed

    payload = crop_json(cli, item_id, "--element-id", "text-0")

    assert set(payload) == {"status", "job_item_id", "count", "directory", "crops"}
    assert payload["count"] == 1
    (single,) = payload["crops"]
    assert set(single) == {
        "element_id", "type", "page", "box", "dpi", "path", "width", "height",
    }


def rewrite_pdf(path, pages=3):
    """Replace the file with a structurally different (still renderable)
    PDF — changed bytes under the same recorded source path."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(612, 792)
    buffer = io.BytesIO()
    doc.save(buffer)
    path.write_bytes(buffer.getvalue())


# --- content drift (issue #119): changed bytes render, with a warning ---


def test_crop_of_a_changed_source_warns_but_still_renders(cli, parsed):
    """A rewritten source is not stale imagery — it renders — but the boxes
    were computed from the original bytes, and the payload must say so."""
    item_id, pdf = parsed
    rewrite_pdf(pdf)

    payload = crop_json(cli, item_id, "--element-id", "text-0")

    assert "changed after" in payload["warning"]
    assert Path(payload["crops"][0]["path"]).is_file()

    human = cli.invoke("crop", item_id, "--element-id", "text-0")
    assert human.exit_code == 0
    assert "warning:" in human.stdout


def test_batch_crop_of_a_changed_source_carries_one_warning(cli, parsed):
    item_id, pdf = parsed
    rewrite_pdf(pdf)

    payload = crop_json(cli, item_id, "--all")

    assert payload["count"] > 0
    assert "changed after" in payload["warning"]
    assert all("warning" not in crop for crop in payload["crops"])


def test_view_crop_of_a_changed_source_carries_the_same_warning(cli, parsed):
    item_id, pdf = parsed
    rewrite_pdf(pdf)

    result = cli.invoke("view", item_id, "--element-id", "text-0", "--crop", "--json")
    assert result.exit_code == 0, result.stdout

    assert "changed after" in json.loads(result.stdout)["warning"]


def test_drift_check_is_best_effort_on_unreadable_sources(tmp_path, monkeypatch):
    """An OS-level read failure while hashing is not drift: the renderer
    owns that failure (CropError / degradation note) — the check returns
    None instead of failing a command the renderer would have degraded."""
    from ade_cli import raster

    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.4 bytes")
    meta = {"source": str(source), "identity": {"content_hash": "0" * 64}}

    def deny(*args, **kwargs):
        raise PermissionError("simulated unreadable source")

    monkeypatch.setattr(raster.hashlib, "file_digest", deny)

    assert raster.source_drift_note(meta) is None


def test_crop_without_a_recorded_hash_stays_quiet(cli, parsed):
    """Items stored before the identity block existed are unverifiable,
    not stale — no warning key at all."""
    item_id, pdf = parsed
    meta_path = cli.home / "jobs" / item_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    del meta["identity"]
    meta_path.write_text(json.dumps(meta))
    rewrite_pdf(pdf)

    payload = crop_json(cli, item_id, "--element-id", "text-0")

    assert "warning" not in payload


def test_batch_crop_of_a_missing_source_fails_whole(cli, parsed):
    """The never-a-stale-image rule scales: a source-level failure stops
    the batch rather than reporting a partial set as a complete one."""
    item_id, pdf = parsed
    pdf.unlink()

    payload = crop_json(cli, item_id, "--all", exit_code=1)

    assert payload["error"] == "source_missing"


def test_batch_crop_through_an_extract_id_resolves_the_parse(cli, parsed, schema_file):
    parse_id, _ = parsed
    extract_id = extract_against(cli, parse_id, schema_file)

    payload = crop_json(cli, extract_id, "--type", "figure")

    # Elements and imagery from the parse; PNGs under the addressed item.
    assert payload["job_item_id"] == extract_id
    assert payload["directory"] == str(cli.home / "jobs" / extract_id / "crops")
    assert [c["element_id"] for c in payload["crops"]] == ["figure-0"]


# --- crop on extract item ids (issue #61): resolve through parse/ref.json ---


def test_crop_of_a_referencing_extract_resolves_through_the_ref(
    cli, parsed, schema_file
):
    parse_id, _ = parsed
    extract_id = extract_against(cli, parse_id, schema_file)

    payload = crop_json(cli, extract_id, "--element-id", "table_cell-0")

    # Elements and imagery come from the referenced parse; the PNG lands
    # under the addressed (extract) item's own crops/.
    (single,) = payload["crops"]
    expected = cli.home / "jobs" / extract_id / "crops" / "table_cell-0@300dpi.png"
    assert single["path"] == str(expected)
    assert expected.is_file()
    assert payload["job_item_id"] == extract_id
    # Same pixel math as cropping via the parse item directly.
    assert single["width"] == int(0.53 * 2550) - int(0.03 * 2550)
    assert single["height"] == int(0.56 * 3300) - int(0.06 * 3300)


def test_crop_via_extract_id_matches_crop_via_parse_id(cli, parsed, schema_file):
    parse_id, _ = parsed
    extract_id = extract_against(cli, parse_id, schema_file)

    via_parse = crop_json(cli, parse_id, "--element-id", "text-0")["crops"][0]
    via_extract = crop_json(cli, extract_id, "--element-id", "text-0")["crops"][0]

    # One shared pipeline: identical geometry, distinct crops/ homes.
    for key in ("type", "page", "box", "dpi", "width", "height"):
        assert via_extract[key] == via_parse[key]
    assert via_parse["path"] != via_extract["path"]


def test_crop_of_extract_whose_parse_was_deleted_is_a_clear_error(
    cli, parsed, schema_file
):
    parse_id, _ = parsed
    extract_id = extract_against(cli, parse_id, schema_file)
    crop_json(cli, extract_id, "--element-id", "text-0")  # a crop exists on disk
    shutil.rmtree(cli.home / "jobs" / parse_id)

    payload = crop_json(cli, extract_id, "--element-id", "text-0", exit_code=1)

    # Never a stale image: the earlier PNG on disk is not an answer.
    assert payload["error"] == "no_parse_linkage"
    assert payload["parse_item_id"] == parse_id
    assert parse_id in payload["message"]


def test_crop_of_extract_with_missing_source_is_source_missing(
    cli, parsed, schema_file
):
    parse_id, pdf = parsed
    extract_id = extract_against(cli, parse_id, schema_file)
    crop_json(cli, extract_id, "--element-id", "text-0")  # a crop exists on disk
    pdf.unlink()

    payload = crop_json(cli, extract_id, "--element-id", "text-0", exit_code=1)

    # The ref resolves to the parse's recorded source; gone ⇒ clear error.
    assert payload["error"] == "source_missing"
    assert "no longer exists" in payload["message"]


def test_crop_of_a_markdown_extract_item_has_no_parse_linkage(cli, tmp_path, schema_file):
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nTotal: €42\n")
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200, completed_extract_job(extract_result(markdown=md.read_text()))
    )
    result = cli.invoke(
        "extract", "--markdown", str(md), "--schema", str(schema_file),
        "--json", env=AUTH_ENV,
    )
    assert result.exit_code == 0, result.stdout
    extract_id = json.loads(result.stdout)["job_item_id"]

    payload = crop_json(cli, extract_id, "--element-id", "text-0", exit_code=1)

    assert payload["error"] == "no_parse_linkage"
    assert "markdown" in payload["message"]


def test_crop_via_extract_id_rejects_an_unknown_element_naming_the_parse(
    cli, parsed, schema_file
):
    parse_id, _ = parsed
    extract_id = extract_against(cli, parse_id, schema_file)

    payload = crop_json(cli, extract_id, "--element-id", "nope-9", exit_code=1)

    # The element namespace is the parse's; the error (and its `find`
    # remediation) points there.
    assert payload["error"] == "unknown_element"
    assert payload["job_item_id"] == parse_id


def test_crop_missing_element_id_hint_names_the_parse_item(cli, parsed, schema_file):
    parse_id, _ = parsed
    extract_id = extract_against(cli, parse_id, schema_file)

    payload = crop_json(cli, extract_id, exit_code=2)

    assert payload["error"] == "missing_element_id"
    # `find` searches parse items, so the discovery hint must name the parse
    # (in find's one calling convention: the job id, positionally).
    assert f"ade find {parse_id}" in payload["message"]


# --- prefix resolution: the one shared resolver behavior ---


def test_crop_resolves_an_unambiguous_prefix(cli, parsed):
    item_id, _ = parsed

    payload = crop_json(cli, item_id[:8], "--element-id", "text-0")

    assert payload["job_item_id"] == item_id


def test_crop_unknown_and_ambiguous_ids_error_with_candidates(cli, parsed):
    # Seeded directly so the shared prefix is deterministic — the scan sees
    # any directory holding metadata, exactly like a real item.
    twins = ("abcd1234aaaaaaaa", "abcd1234bbbbbbbb")
    for item_id in twins:
        d = cli.home / "jobs" / item_id
        d.mkdir(parents=True)
        (d / "meta.json").write_text(
            json.dumps({"kind": "parse", "state": "parsed", "source": "x.pdf"})
        )

    ambiguous = crop_json(cli, "abcd1234", "--element-id", "text-0", exit_code=2)
    assert ambiguous["error"] == "ambiguous_id"
    assert set(ambiguous["candidates"]) == set(twins)

    unknown = crop_json(cli, "zzzzzzzz", "--element-id", "text-0", exit_code=1)
    assert unknown["error"] == "unknown_id"
    assert "history list" in unknown["message"]


def test_view_crop_flag_runs_the_same_pipeline(cli, parsed):
    item_id, _ = parsed

    result = cli.invoke("view", item_id, "--element-id", "text-0", "--crop", "--json")
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)

    assert payload["status"] == "cropped"
    assert payload["path"].endswith("crops/text-0@300dpi.png")
    # ...plus the dedicated single-crop artifact: crop as media, the
    # element's parse.json slice, and only the fields citing this element.
    assert payload["html"].endswith("crops/text-0@300dpi.html")
    html = (cli.home / "jobs" / item_id / "crops" / "text-0@300dpi.html").read_text()
    data = json.loads(
        html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    )
    assert data["element_id"] == "text-0"
    assert data["image"].startswith("data:image/png;base64,")
    assert data["element_json"]["id"] == "text-0"
    assert data["fields"] == []  # no stored extraction in this fixture

    missing = cli.invoke("view", item_id, "--crop", "--json")
    assert missing.exit_code == 2
    assert json.loads(missing.stdout)["error"] == "missing_element_id"


def test_view_crop_on_a_referencing_extract_renders_from_the_parse_source(
    cli, parsed, schema_file
):
    parse_id, _ = parsed
    extract_id = extract_against(cli, parse_id, schema_file)

    result = cli.invoke(
        "view", extract_id, "--element-id", "text-0", "--crop", "--json"
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)

    # Same through-the-ref rule as the standalone `crop`: imagery from the
    # parse item's source, artifacts under the extract item's crops/.
    assert payload["status"] == "cropped"
    assert payload["job_item_id"] == extract_id
    expected = cli.home / "jobs" / extract_id / "crops" / "text-0@300dpi.png"
    assert payload["path"] == str(expected)
    assert expected.is_file()


def test_single_crop_output_path_creates_missing_parents(cli, parsed, tmp_path):
    item_id, _ = parsed
    out = tmp_path / "nested" / "never-made" / "cell.png"

    payload = crop_json(cli, item_id, "--element-id", "table_cell-0", "-o", str(out))

    assert out.is_file()
    assert payload["crops"][0]["path"] == str(out)
    assert payload["directory"] == str(out.parent)
