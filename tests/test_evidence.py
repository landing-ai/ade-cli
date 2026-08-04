"""Evidence join (issue #8): field→box evidence as a local join — extraction
spans and element spans index the same markdown, so each quoted field
resolves to overlapping elements ⇒ pages and boxes, with zero API calls.

Every test drives the CLI seam; evidence content is checked for
consistency against the stored elements projection. Since the job-item
store (#58) the extract item references its parse via ``parse/ref.json``
and the join resolves through that edge.
"""

import json

import pytest

from extract_fixtures import SCHEMA, completed_extract_job, extract_result
from parse_fixtures import JOB_ID, completed_job, rich_parse_response

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}
DOC_BYTES = b"%PDF-1.4 fake invoice bytes"

RICH = rich_parse_response()
RICH_MARKDOWN = RICH["markdown"]


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(DOC_BYTES)
    return path


@pytest.fixture
def schema_file(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(SCHEMA))
    return path


def parse_rich_doc(cli, document, *, force=False, job_id=JOB_ID):
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_job(rich_parse_response(job_id=job_id)))
    args = ["parse", "-d", str(document), "--json"] + (["--force"] if force else [])
    result = cli.invoke(*args, env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


def span_of(needle: str) -> dict:
    at = RICH_MARKDOWN.find(needle)
    assert at >= 0, f"{needle!r} not in the rich fixture markdown"
    return {"start": at, "end": at + len(needle)}


def run_extract(cli, *args, metadata: dict, markdown: str = RICH_MARKDOWN):
    extraction = {path: leaf["value"] for path, leaf in metadata.items()}
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200,
        completed_extract_job(
            extract_result(
                markdown=markdown,
                extraction=extraction,
                extraction_metadata=metadata,
            )
        ),
    )
    result = cli.invoke("extract", *args, "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def evidence_path(cli, payload):
    return cli.home / "jobs" / payload["job_item_id"] / "evidence.json"


def field(evidence_fields: list[dict], name: str) -> dict:
    (record,) = [f for f in evidence_fields if f["field"] == name]
    return record


def test_quoted_fields_yield_elements_pages_boxes_consistent_with_projection(
    cli, document, schema_file
):
    parse_id = parse_rich_doc(cli, document)
    payload = run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={"total": {"value": "€42", "ranges": [span_of("€42")]}},
    )

    assert payload["evidence"]["kind"] == "grounded"
    record = field(payload["evidence"]["fields"], "total")
    # "€42" lives inside the second-page text element of the rich fixture
    # (pages are 1-indexed on the wire).
    assert record["element_ids"] == ["text-1"]
    assert record["pages"] == [2]

    # boxes/pages must agree with the stored elements projection
    elements = json.loads(
        (cli.home / "jobs" / parse_id / "elements.json").read_text()
    )["elements"]
    (text_1,) = [e for e in elements if e["id"] == "text-1"]
    assert record["boxes"] == [{"page": text_1["page"], "box": text_1["box"]}]

    # persisted as the recomputable evidence artifact, stamped by generation
    on_disk = json.loads(evidence_path(cli, payload).read_text())
    assert on_disk["kind"] == "grounded"
    assert on_disk["job_id"] == payload["run_id"]
    assert field(on_disk["fields"], "total") == record


def test_ungroundable_fields_carry_the_explicit_marker(cli, document, schema_file):
    parse_id = parse_rich_doc(cli, document)
    payload = run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={
            "total": {"value": "€42", "ranges": [span_of("€42")]},
            "vendor": {"value": "Acme Corp", "ranges": None},
        },
    )

    record = field(payload["evidence"]["fields"], "vendor")
    assert record == {"field": "vendor", "value": "Acme Corp", "ungroundable": True}
    on_disk = json.loads(evidence_path(cli, payload).read_text())
    assert field(on_disk["fields"], "vendor")["ungroundable"] is True


def test_empty_valued_fields_carry_the_empty_marker_not_ungroundable(
    cli, document, schema_file
):
    # F5: null spans split by whether there is anything to ground — an
    # empty value ("" or null) is empty by nature; ungroundable stays
    # reserved for non-empty values with no located evidence. False/0 are
    # real values, so a spanless False stays ungroundable.
    parse_id = parse_rich_doc(cli, document)
    payload = run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={
            "note": {"value": "", "ranges": None},
            "status": {"value": None, "ranges": []},
            "approved": {"value": False, "ranges": None},
        },
    )

    fields = payload["evidence"]["fields"]
    assert field(fields, "note") == {"field": "note", "value": "", "empty": True}
    assert field(fields, "status") == {"field": "status", "value": None, "empty": True}
    assert field(fields, "approved") == {
        "field": "approved", "value": False, "ungroundable": True,
    }
    on_disk = json.loads(evidence_path(cli, payload).read_text())
    assert field(on_disk["fields"], "note")["empty"] is True
    assert "ungroundable" not in field(on_disk["fields"], "note")


def test_spans_crossing_element_boundaries_resolve_to_all_overlapping_elements(
    cli, document, schema_file
):
    parse_id = parse_rich_doc(cli, document)
    # One span from inside the first-page heading into the table's first cell:
    # it must resolve to every overlapping element — heading, the table
    # container, and the cell — never just the first hit.
    start = RICH_MARKDOWN.find("Invoice")
    end = RICH_MARKDOWN.find("Qty") + len("Qty")
    payload = run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={
            "header": {
                "value": RICH_MARKDOWN[start:end],
                "ranges": [{"start": start, "end": end}],
            }
        },
    )

    record = field(payload["evidence"]["fields"], "header")
    assert set(record["element_ids"]) == {"text-0", "table-0", "table_cell-0"}
    assert record["pages"] == [1]
    assert len(record["boxes"]) == 3


def test_markdown_sourced_extraction_degrades_to_labeled_spans_only(
    cli, tmp_path, schema_file
):
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nTotal: €42\n")
    text = md.read_text()
    at = text.find("€42")
    payload = run_extract(
        cli,
        "--markdown", str(md), "--schema", str(schema_file),
        markdown=text,
        metadata={"total": {"value": "€42", "ranges": [{"start": at, "end": at + 3}]}},
    )

    assert payload["evidence"]["kind"] == "spans_only"  # no grounding to join against
    assert payload["evidence"]["reason"] == "markdown_doc"  # cause labeled in --json too
    record = field(payload["evidence"]["fields"], "total")
    assert record["spans"] == [[at, at + 3]]
    assert "element_ids" not in record
    assert "boxes" not in record
    on_disk = json.loads(evidence_path(cli, payload).read_text())
    assert on_disk["kind"] == "spans_only"


def test_evidence_is_recomputable_from_raw_artifacts_alone(cli, document, schema_file):
    parse_id = parse_rich_doc(cli, document)
    payload = run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={
            "total": {"value": "€42", "ranges": [span_of("€42")]},
            "vendor": {"value": "Acme Corp", "ranges": None},
        },
    )
    path = evidence_path(cli, payload)
    on_disk = json.loads(path.read_text())
    path.unlink()  # derived is disposable; raw artifacts remain
    seen = len(cli.transport.requests)

    again = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert again.exit_code == 0
    assert len(cli.transport.requests) == seen  # a local join, zero API calls
    recomputed = json.loads(again.stdout)
    assert recomputed["cached"] is True
    assert recomputed["evidence"]["kind"] == on_disk["kind"]
    assert recomputed["evidence"]["fields"] == on_disk["fields"]
    assert not path.exists()  # recomputed in memory; reads never write


def test_stale_extraction_keeps_stored_evidence_and_labels_a_lost_generation(
    cli, document, schema_file
):
    parse_id = parse_rich_doc(cli, document)
    payload = run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={"total": {"value": "€42", "ranges": [span_of("€42")]}},
    )
    path = evidence_path(cli, payload)
    grounded = json.loads(path.read_text())

    # A forced re-parse replaces the parse item's generation in place (new
    # server job): the referencing extract item goes stale.
    parse_rich_doc(cli, document, force=True, job_id="job-0002")

    # Stored evidence survives — still true of the bytes it was computed
    # from; nothing rewrites it behind the extraction's back.
    assert json.loads(path.read_text()) == grounded
    assert grounded["kind"] == "grounded"

    # Without it, that generation's grounding is gone: the recompute
    # degrades to spans-only and states the cause — never a silent join
    # against the new parse's markdown. (view is the stale item's read
    # surface; its layer payload carries the recomputed evidence.)
    path.unlink()
    seen = len(cli.transport.requests)
    # Parse viewers hold parse only; the stale layer renders in the extract
    # item's own viewer.
    viewed = cli.invoke(
        "view", payload["job_item_id"], "--no-sidebar-sync", "--json"
    )
    assert viewed.exit_code == 0, viewed.stdout
    assert len(cli.transport.requests) == seen  # a local join, zero API calls
    html = (
        cli.home / "jobs" / payload["job_item_id"] / "view.html"
    ).read_text(encoding="utf-8")
    data = json.loads(
        html.split('type="application/json">', 1)[1].split("</script>", 1)[0]
    )
    (extraction,) = data["extractions"]
    assert extraction["stale"] is True
    assert extraction["evidence"]["kind"] == "spans_only"
    assert extraction["evidence"]["reason"] == "parse_replaced"


def test_a_quoted_span_overlapping_no_element_keeps_explicit_empty_evidence(
    cli, document, schema_file
):
    parse_id = parse_rich_doc(cli, document)
    # The doc_id trailer is inside no element's span: a quoted value landing
    # there yields explicitly empty lists — distinct from ungroundable.
    trailer = span_of("srv-doc-77aa00")
    payload = run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={"total": {"value": "srv-doc-77aa00", "ranges": [trailer]}},
    )

    record = field(payload["evidence"]["fields"], "total")
    assert record["element_ids"] == []
    assert record["pages"] == []
    assert record["boxes"] == []
    assert "ungroundable" not in record


def test_cached_rerun_still_reports_evidence(cli, document, schema_file):
    parse_id = parse_rich_doc(cli, document)
    run_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        metadata={"total": {"value": "€42", "ranges": [span_of("€42")]}},
    )
    seen = len(cli.transport.requests)

    again = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert again.exit_code == 0
    assert len(cli.transport.requests) == seen
    payload = json.loads(again.stdout)
    assert payload["cached"] is True
    assert field(payload["evidence"]["fields"], "total")["element_ids"] == ["text-1"]

    human = cli.invoke("extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV)
    assert human.exit_code == 0
    assert "grounded" in human.stdout
