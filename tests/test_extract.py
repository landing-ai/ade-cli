"""The extract guarantee: lifecycle reuse & staleness (issue #7; flat
job-item store per the 2026-07-21 revision).

Extract runs on the shared guarantee machinery. Every extraction is its
own top-level job item: input is a parse job item id (the extract item
*references* the parse via ``parse/ref.json``) or bring-your-own markdown
(copied in as ``markdown.md``). Every test drives the CLI seam;
billing-visible behavior (submits) is asserted on the fake transport.
"""

import json

import pytest

from ade_cli import store as jobstore

from extract_fixtures import SCHEMA, completed_extract_job, extract_result
from parse_fixtures import (
    JOB_ID,
    MARKDOWN,
    completed_job,
    job_payload,
    parse_response,
)

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}
DOC_BYTES = b"%PDF-1.4 fake invoice bytes"


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


def posts(cli, path=None):
    return [
        r
        for r in cli.transport.requests
        if r.method == "POST" and (path is None or r.url.path == path)
    ]


def extract_posts(cli):
    return posts(cli, "/v2/extract/jobs")


def parse_doc(cli, document):
    """Seed a completed parse job item; returns its job item id."""
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    assert result.exit_code == 0
    return json.loads(result.stdout)["job_item_id"]


def complete_extract(cli, *args, job_id="extract-0001", result=None):
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_extract_job(result, job_id=job_id))
    invoked = cli.invoke("extract", *args, "--json", env=AUTH_ENV)
    assert invoked.exit_code == 0, invoked.stdout
    return json.loads(invoked.stdout)


def extract_item_dirs(cli):
    """Top-level extract job items — the store is flat; kind rides on the
    claim ticket."""
    return sorted(
        d
        for d in (cli.home / "jobs").iterdir()
        if d.is_dir()
        and (d / "job.json").exists()
        and json.loads((d / "job.json").read_text()).get("kind") == "extract"
    )


def test_same_item_and_schema_twice_is_one_submit_total(cli, document, schema_file):
    parse_id = parse_doc(cli, document)
    first = complete_extract(cli, parse_id, "--schema", str(schema_file))
    seen = len(cli.transport.requests)

    again = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert again.exit_code == 0
    assert len(cli.transport.requests) == seen  # served from disk
    payload = json.loads(again.stdout)
    assert payload["status"] == "extracted"
    assert payload["cached"] is True
    assert payload["run_id"] == first["run_id"]  # still traceable to the bill
    assert len(extract_posts(cli)) == 1


def test_cached_extract_echoes_the_original_bill(cli, document, schema_file):
    # Same contract as parse: the free cached hit reports the original
    # run's bill next to cached=true, never a zero.
    parse_id = parse_doc(cli, document)
    first = complete_extract(
        cli, parse_id, "--schema", str(schema_file),
        result=extract_result(total_credits=1.1),
    )

    again = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    payload = json.loads(again.stdout)
    assert first["credits"] == 1.1
    assert payload["cached"] is True
    assert payload["credits"] == 1.1  # the original bill, never zeroed
    assert payload["tier"] == first["tier"]


def test_extract_item_references_the_parse_never_copies_it(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)

    payload = complete_extract(cli, parse_id, "--schema", str(schema_file))

    item_dir = cli.home / "jobs" / payload["job_item_id"]
    assert payload["job_item_id"] != parse_id  # its own top-level job item
    assert payload["parse_job_item_id"] == parse_id
    ref = json.loads((item_dir / "parse/ref.json").read_text())
    assert ref == {"job_item_id": parse_id, "parse_job_id": JOB_ID}
    # One copy of ground truth: the parse artifacts stay in the parse item.
    assert not (item_dir / "parse.json").exists()
    assert not (item_dir / "parse" / "parse.json").exists()


def test_extracting_two_parse_variants_mints_sibling_extract_items(
    cli, document, schema_file
):
    # Parse variants share source and content; the extract identity carries
    # the parse linkage so their extractions never collide on one id (which
    # would silently re-run in place — variants coexist, nothing replaced).
    variant_a = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    slow = cli.invoke(
        "parse", "-d", str(document), "--tier", "standard", "--json", env=AUTH_ENV
    )
    assert slow.exit_code == 0
    variant_b = json.loads(slow.stdout)["job_item_id"]
    assert variant_a != variant_b

    first = complete_extract(cli, variant_a, "--schema", str(schema_file))
    second = complete_extract(
        cli, variant_b, "--schema", str(schema_file), job_id="extract-0002"
    )

    assert first["job_item_id"] != second["job_item_id"]  # siblings, no clobber
    assert len(extract_item_dirs(cli)) == 2
    ref_a = json.loads(
        (cli.home / "jobs" / first["job_item_id"] / "parse/ref.json").read_text()
    )
    assert ref_a["job_item_id"] == variant_a


def test_different_schema_is_a_sibling_extract_item_and_both_listed(
    cli, document, schema_file, tmp_path
):
    parse_id = parse_doc(cli, document)
    first = complete_extract(cli, parse_id, "--schema", str(schema_file))

    other_schema = tmp_path / "other-schema.json"
    other_schema.write_text(
        json.dumps({"type": "object", "properties": {"date": {"type": "string"}}})
    )
    second = complete_extract(
        cli, parse_id, "--schema", str(other_schema), job_id="extract-0002"
    )

    assert first["job_item_id"] != second["job_item_id"]
    assert len(extract_posts(cli)) == 2  # different schema ⇒ deliberate new submit
    assert len(extract_item_dirs(cli)) == 2

    listed = cli.invoke("history", "list", "--json", env=AUTH_ENV)
    records = {r["job_item_id"]: r for r in json.loads(listed.stdout)}
    for payload in (first, second):
        record = records[payload["job_item_id"]]
        assert record["state"] == "extracted"
        assert record["parse"]["job_item_id"] == parse_id


def test_extracting_a_pending_item_errors_naming_parse_with_no_api_call(
    cli, document, schema_file
):
    # The item is stored but pending — no completed parse exists.
    cli.transport.respond(202, {"job_id": JOB_ID})
    pending = cli.invoke(
        "parse", "-d", str(document), "--wait", "0", "--json", env=AUTH_ENV
    )
    assert pending.exit_code == 3
    parse_id = json.loads(pending.stdout)["job_item_id"]
    seen = len(cli.transport.requests)

    result = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert result.exit_code == 1
    assert len(cli.transport.requests) == seen  # no API call, no hidden auto-parse
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_parsed"
    assert payload["state"] == "pending"

    human = cli.invoke("extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV)
    assert human.exit_code == 1
    assert "ade parse" in human.stdout
    assert len(cli.transport.requests) == seen


def test_small_markdown_is_sent_inline_without_staging(cli, tmp_path, schema_file):
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nTotal: €42\n")
    payload = complete_extract(cli, "--markdown", str(md), "--schema", str(schema_file))

    # Identity: verb x markdown source x bytes x extract params.
    assert payload["job_item_id"] == jobstore.derive_id(
        "extract",
        "production",
        jobstore.local_identity(md, md.read_bytes()),
        {"schema": SCHEMA, "model": "extract-latest", "options": {}},
    )
    assert posts(cli, "/v1/files") == []  # small stays inline
    (submit,) = extract_posts(cli)
    body = json.loads(submit.content)
    assert body["markdown"] == "# Notes\n\nTotal: €42\n"
    assert "markdown_ref" not in body
    assert body["schema"] == SCHEMA

    # The input markdown is copied into the item: spans index exactly
    # these bytes (decision 9); and the item is a real store citizen.
    item_dir = cli.home / "jobs" / payload["job_item_id"]
    assert (item_dir / "markdown.md").read_text() == "# Notes\n\nTotal: €42\n"
    listed = cli.invoke("history", "list", "--json", env=AUTH_ENV)
    (record,) = json.loads(listed.stdout)
    assert record["job_item_id"] == payload["job_item_id"]
    assert record["kind"] == "extract"
    assert record["state"] == "extracted"
    assert record["parse"] is None  # no parse edge, can never go stale


def test_large_markdown_rides_as_a_multipart_file_part(cli, tmp_path, schema_file):
    big = "# Big\n\n" + ("x" * 1_200_000) + "\n"
    md = tmp_path / "big.md"
    md.write_text(big)

    payload = complete_extract(cli, "--markdown", str(md), "--schema", str(schema_file))

    # The contract's large-input path: a multipart FILE part named markdown
    # (the gateway stages the upload internally; the public request takes no
    # *_ref fields). No /v1/files call, no inline JSON body.
    assert posts(cli, "/v1/files") == []
    (submit,) = extract_posts(cli)
    assert submit.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="markdown.md"' in submit.content
    assert big.encode() in submit.content
    assert json.dumps(SCHEMA).encode() in submit.content  # JSON-serialized form field
    assert payload["status"] == "extracted"


def test_a_retried_submit_repeats_the_same_body_and_idempotency_key(
    cli, tmp_path, schema_file
):
    big = "# Big\n\n" + ("x" * 1_200_000) + "\n"
    md = tmp_path / "big.md"
    md.write_text(big)

    cli.transport.respond(429, {"detail": "busy"}, headers={"Retry-After": "3"})
    payload = complete_extract(cli, "--markdown", str(md), "--schema", str(schema_file))
    assert payload["status"] == "extracted"

    first, second = extract_posts(cli)
    # one claim generation ⇒ one key paired with one body: the retry repeats
    # the identical upload, so the server can attach it to the job it
    # already accepted once the idempotency ask lands
    assert big.encode() in first.content
    assert big.encode() in second.content
    assert (
        first.headers["idempotency-key"] == second.headers["idempotency-key"]
    )


def test_extracting_a_markdown_extract_item_errors_as_not_a_parse_item(
    cli, tmp_path, schema_file
):
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nTotal: €42\n")
    payload = complete_extract(cli, "--markdown", str(md), "--schema", str(schema_file))
    seen = len(cli.transport.requests)

    # An extract item's id can never be extract's JOB_ID input; the error
    # names the mistake instead of dead-ending at a parse command.
    result = cli.invoke(
        "extract", payload["job_item_id"], "--schema", str(schema_file),
        "--json", env=AUTH_ENV,
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "not_a_parse_item"
    assert len(cli.transport.requests) == seen  # still no API call


def test_forced_reparse_marks_extraction_stale_and_same_schema_reextracts(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    first = complete_extract(cli, parse_id, "--schema", str(schema_file))

    # A forced re-parse replaces the parse item's artifacts in place — the
    # one remaining staleness cause (variants are siblings, never stale).
    new_markdown = "# Reparsed invoice\n\nTotal: €99\n\n<!-- doc_id=srv-doc-77aa00 -->\n"
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(
        200, completed_job(parse_response(markdown=new_markdown, job_id="job-0002"))
    )
    reparsed = cli.invoke("parse", "-d", str(document), "--force", env=AUTH_ENV)
    assert reparsed.exit_code == 0

    # The extract item's recorded parse generation no longer matches.
    item_dir = cli.home / "jobs" / first["job_item_id"]
    ref = json.loads((item_dir / "parse/ref.json").read_text())
    assert ref["parse_job_id"] == JOB_ID  # the generation it ran against
    parse_meta = json.loads((cli.home / "jobs" / parse_id / "meta.json").read_text())
    assert parse_meta["job_id"] == "job-0002"  # ...which is gone

    # Same invocation again: stale ⇒ re-extract in place (and re-bill),
    # against the new markdown; the ref records the new generation.
    payload = complete_extract(
        cli,
        parse_id, "--schema", str(schema_file),
        job_id="extract-0002",
        result=extract_result(markdown=new_markdown, job_id="extract-0002"),
    )
    assert payload["cached"] is False
    assert payload["run_id"] == "extract-0002"
    assert payload["job_item_id"] == first["job_item_id"]  # same item, re-run
    assert len(extract_posts(cli)) == 2
    assert json.loads(extract_posts(cli)[-1].content)["markdown"] == new_markdown
    ref = json.loads((item_dir / "parse/ref.json").read_text())
    assert ref["parse_job_id"] == "job-0002"


def test_ungroundable_fields_are_flagged_in_summary_and_json(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    payload = complete_extract(cli, parse_id, "--schema", str(schema_file))
    assert payload["ungroundable"] == ["vendor"]  # null ranges ⇒ synthesised
    assert payload["empty_fields"] == []

    human = cli.invoke("extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV)
    assert human.exit_code == 0  # cached — still reports groundability
    assert "ungroundable" in human.stdout
    assert "vendor" in human.stdout


def test_empty_valued_fields_read_as_empty_not_ungroundable(
    cli, document, schema_file
):
    # F5: a blank cell has no box an empty string could ground to —
    # expected, reported quietly; ungroundable stays reserved for non-empty
    # values whose evidence could not be located.
    parse_id = parse_doc(cli, document)
    at = MARKDOWN.find("€42")
    result = extract_result(
        extraction={"total": "€42", "vendor": "Acme Corp", "note": "", "status": None},
        extraction_metadata={
            "total": {"value": "€42", "ranges": [{"start": at, "end": at + 3}]},
            "vendor": {"value": "Acme Corp", "ranges": None},
            "note": {"value": "", "ranges": None},
            "status": {"value": None, "ranges": None},
        },
    )
    payload = complete_extract(cli, parse_id, "--schema", str(schema_file), result=result)

    assert payload["ungroundable"] == ["vendor"]  # the alarm stays precise
    assert payload["empty_fields"] == ["note", "status"]

    human = cli.invoke("extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV)
    assert human.exit_code == 0
    assert "1 ungroundable: vendor" in human.stdout
    assert "2 empty — no value to ground" in human.stdout


def test_idempotency_key_is_sent_and_survives_claim_recovery(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    complete_extract(cli, parse_id, "--schema", str(schema_file))
    (first_submit,) = extract_posts(cli)
    key = first_submit.headers["idempotency-key"]
    assert key

    # Crash window: the submit POST was accepted server-side but the process
    # died before recording job_id. The ticket is submitless and past its
    # lease; the extraction artifacts never got written.
    (extract_dir,) = extract_item_dirs(cli)
    ticket = json.loads((extract_dir / "job.json").read_text())
    ticket.update({"job_id": None, "state": "pending", "submitted_at": 0})
    (extract_dir / "job.json").write_text(json.dumps(ticket))
    (extract_dir / "meta.json").unlink()
    (extract_dir / "extract.json").unlink()

    complete_extract(cli, parse_id, "--schema", str(schema_file))

    retried = extract_posts(cli)[-1]
    # Same claim generation ⇒ same key: the retried submit attaches to the
    # job the server already accepted instead of billing a duplicate.
    assert retried.headers["idempotency-key"] == key


def test_extract_poll_rides_out_a_transient_5xx(cli, document, schema_file):
    # The 5xx-as-pending-tick behavior (#19) lives in the shared lifecycle,
    # so the extract poll route gets it too — pinned here on its own route.
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(500, None)
    cli.transport.respond(200, completed_extract_job(job_id="extract-0001"))

    result = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "extract-0001"
    assert len(extract_posts(cli)) == 1  # never a blind resubmit


def test_wait_zero_saves_the_ticket_and_rerun_resumes_without_resubmit(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    first = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--wait", "0", "--json",
        env=AUTH_ENV,
    )
    assert first.exit_code == 3  # pending is a normal outcome, not an error
    payload = json.loads(first.stdout)
    assert payload["status"] == "pending"
    assert payload["run_id"] == "extract-0001"
    (extract_dir,) = extract_item_dirs(cli)
    assert json.loads((extract_dir / "job.json").read_text())["job_id"] == "extract-0001"

    cli.transport.respond(200, completed_extract_job())
    second = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )
    assert second.exit_code == 0
    assert len(extract_posts(cli)) == 1  # exactly one submit across both runs


def test_unsupported_extract_result_fails_explicitly_before_any_write(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    drifted = extract_result()
    del drifted["extraction_metadata"]  # e.g. a future contract renamed it
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job(drifted))

    result = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "unsupported_result_schema"
    (extract_dir,) = extract_item_dirs(cli)
    # Rejected whole, before any artifact write — no torn extract set.
    assert not (extract_dir / "extract.json").exists()
    assert not (extract_dir / "meta.json").exists()
    ticket = json.loads((extract_dir / "job.json").read_text())
    assert ticket["state"] == "unreadable"
    assert "extraction_metadata" in ticket["reason"]


def test_wrong_typed_extract_metadata_fails_explicitly_before_any_write(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    drifted = extract_result()
    drifted["metadata"] = "extract-20270101"  # wrong type: .get would
    cli.transport.respond(202, {"job_id": "extract-0001"})  # crash post-write
    cli.transport.respond(200, completed_extract_job(drifted))

    result = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "unsupported_result_schema"
    (extract_dir,) = extract_item_dirs(cli)
    assert not (extract_dir / "extract.json").exists()
    assert not (extract_dir / "evidence.json").exists()


def test_failed_extract_is_reported_once_then_resubmitted_fresh(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200,
        job_payload("failed", job_id="extract-0001", failure_reason="schema too deep"),
    )
    first = cli.invoke("extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV)
    assert first.exit_code == 1
    assert "schema too deep" in first.stdout

    payload = complete_extract(
        cli, parse_id, "--schema", str(schema_file), job_id="extract-0002"
    )
    assert payload["run_id"] == "extract-0002"
    assert len(extract_posts(cli)) == 2  # one fresh resubmit, not a cache hit


def test_unreadable_extract_completion_rejoins_the_same_job_never_resubmits(
    cli, document, schema_file
):
    # Same shared-lifecycle posture as parse: a completed job the CLI can't
    # read marks the ticket unreadable, and the re-run re-polls — a fresh
    # submit would re-bill a job that reads the same way.
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200, {**job_payload("completed", job_id="extract-0001"), "data": {}}
    )
    first = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )
    assert first.exit_code == 1
    payload = json.loads(first.stdout)
    assert payload["error"] == "missing_result"
    assert "data" in payload["payload_keys"]
    (extract_dir,) = extract_item_dirs(cli)
    assert json.loads((extract_dir / "job.json").read_text())["state"] == "unreadable"

    cli.transport.respond(200, completed_extract_job(job_id="extract-0001"))
    second = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )
    assert second.exit_code == 0
    assert json.loads(second.stdout)["run_id"] == "extract-0001"
    assert len(extract_posts(cli)) == 1  # exactly one submit across both runs


def test_markdown_url_is_passed_through(cli, schema_file):
    url = "https://example.com/notes.md"
    payload = complete_extract(cli, "--markdown-url", url, "--schema", str(schema_file))

    # URL sources have no content component — identity is the URL x params.
    assert payload["job_item_id"] == jobstore.derive_id(
        "extract",
        "production",
        jobstore.url_identity(url),
        {"schema": SCHEMA, "model": "extract-latest", "options": {}},
    )
    (submit,) = extract_posts(cli)
    body = json.loads(submit.content)
    assert body["markdown_url"] == url
    assert "markdown" not in body
    # The response's echoed markdown is materialized as the item's input
    # contract — the CLI never had a local file (decision 9).
    item_dir = cli.home / "jobs" / payload["job_item_id"]
    assert (item_dir / "markdown.md").exists()


def test_markdown_url_without_a_string_echo_fails_before_any_write(cli, schema_file):
    # markdown.md materializes from the response's echoed markdown; a
    # missing echo must fail whole — never persist an empty input contract
    # under spans that indexed different bytes.
    drifted = extract_result()
    del drifted["markdown"]
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job(drifted))

    result = cli.invoke(
        "extract", "--markdown-url", "https://example.com/notes.md",
        "--schema", str(schema_file), "--json", env=AUTH_ENV,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "unsupported_result_schema"
    assert "markdown echo" in payload["reason"]
    (extract_dir,) = extract_item_dirs(cli)
    assert not (extract_dir / "markdown.md").exists()
    assert not (extract_dir / "extract.json").exists()
    assert json.loads((extract_dir / "job.json").read_text())["state"] == "unreadable"


def test_exactly_one_input_is_required(cli, document, schema_file, tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("hello")
    both = cli.invoke(
        "extract", "some-item-id", "--markdown", str(md), "--schema", str(schema_file),
        env=AUTH_ENV,
    )
    assert both.exit_code == 2

    neither = cli.invoke("extract", "--schema", str(schema_file), env=AUTH_ENV)
    assert neither.exit_code == 2


def test_inline_schema_and_option_flags_reach_the_wire(cli, document):
    parse_id = parse_doc(cli, document)
    inline = json.dumps({"type": "object", "properties": {"total": {"type": "string"}}})
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())
    result = cli.invoke(
        "extract",
        parse_id, "--schema", inline,
        "--tier", "standard", "--strict", "--model", "extract-20260630",
        env=AUTH_ENV,
    )
    assert result.exit_code == 0

    (submit,) = extract_posts(cli)
    body = json.loads(submit.content)
    assert body["schema"] == json.loads(inline)
    assert body["service_tier"] == "standard"
    assert body["options"] == {"strict": True}
    assert body["model"] == "extract-20260630"


def test_a_schema_that_is_neither_a_file_nor_json_is_a_usage_error(cli, document):
    result = cli.invoke(
        "extract", "some-item-id", "--schema", "no-such-file.json", env=AUTH_ENV
    )
    assert result.exit_code == 2
    assert "schema" in result.stdout.lower()


def test_an_empty_schema_is_refused_before_any_api_call(cli, document):
    """#154: an empty-properties schema is valid JSON Schema, but the
    server would process (and bill) the whole document to extract nothing
    — refused locally, before submit, like the --pages/--options conflict."""
    parse_id = parse_doc(cli, document)
    for spec in ('{"type": "object", "properties": {}}', '{"type": "object"}'):
        result = cli.invoke(
            "extract", parse_id, "--schema", spec, "--json", env=AUTH_ENV
        )
        assert result.exit_code == 2, result.stdout
        payload = json.loads(result.stdout)
        assert payload["error"] == "empty_schema"
        assert "not sent" in payload["message"]
    # Nothing beyond the seeded parse ever reached the transport.
    assert extract_posts(cli) == []


@pytest.mark.parametrize(
    "schema",
    [
        # Composition with real fields somewhere reachable must submit —
        # a false block would be worse than a billed empty run.
        {"type": "object", "allOf": [{"properties": {"total": {"type": "string"}}}]},
        {"type": "object", "anyOf": [{}, {"properties": {"total": {"type": "string"}}}]},
        {"type": "array", "items": {"properties": {"total": {"type": "string"}}}},
        {"type": "object", "patternProperties": {"^x-": {"type": "string"}}},
        # $ref is unresolvable locally: give the server the benefit.
        {"$ref": "#/$defs/invoice", "$defs": {"invoice": {"properties": {}}}},
    ],
)
def test_a_composing_schema_with_reachable_fields_still_submits(
    cli, document, schema
):
    """The empty-schema gate must not false-positive on composition — a
    $ref/allOf/items schema has fields even with no top-level properties
    map."""
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())
    result = cli.invoke(
        "extract", parse_id, "--schema", json.dumps(schema), env=AUTH_ENV
    )
    assert result.exit_code == 0, result.stdout


@pytest.mark.parametrize(
    "schema",
    [
        # Empty composition shells define nothing — mere presence of a
        # composition key must not bypass the gate.
        {"type": "object", "allOf": []},
        {"type": "object", "allOf": [{}]},
        {"type": "object", "anyOf": [{"type": "string"}]},
        {"type": "object", "patternProperties": {}},
        {"type": "array", "items": {}},
        {"type": "object", "properties": {}, "allOf": [{"properties": {}}]},
    ],
)
def test_an_empty_composition_shell_is_still_refused(cli, document, schema):
    parse_id = parse_doc(cli, document)
    seen = len(cli.transport.requests)

    result = cli.invoke(
        "extract", parse_id, "--schema", json.dumps(schema), "--json", env=AUTH_ENV
    )

    assert result.exit_code == 2, result.stdout
    assert json.loads(result.stdout)["error"] == "empty_schema"
    assert len(cli.transport.requests) == seen  # nothing submitted


# An inline schema longer than a filesystem name component (255 bytes on
# macOS/Linux): the file probe must not blow up on it (#143).
LONG_INLINE_SCHEMA = {
    "type": "object",
    "properties": {f"survey_question_{i:02d}": {"type": "string"} for i in range(20)},
}


def test_long_inline_schema_parses_as_inline_json(cli, document):
    parse_id = parse_doc(cli, document)
    inline = json.dumps(LONG_INLINE_SCHEMA)
    assert len(inline) > 255  # past the ENAMETOOLONG threshold (#143)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())
    result = cli.invoke("extract", parse_id, "--schema", inline, env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout

    (submit,) = extract_posts(cli)
    assert json.loads(submit.content)["schema"] == LONG_INLINE_SCHEMA


def test_long_inline_non_json_schema_is_a_structured_usage_error(cli, document):
    # Longer than any filename component and not JSON: previously an
    # unhandled OSError; must be the same structured usage error a short
    # bad spec gets (#143).
    spec = "not json " * 40
    assert len(spec) > 255
    result = cli.invoke(
        "extract", "some-item-id", "--schema", spec, "--json", env=AUTH_ENV
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "bad_schema"


def test_an_unreadable_schema_file_is_a_structured_usage_error(
    cli, document, tmp_path, monkeypatch
):
    # An OS-level read failure, simulated rather than provoked via
    # chmod(0) — root (containerized CI) reads through permission bits.
    locked = tmp_path / "locked-schema.json"
    locked.write_text(json.dumps(SCHEMA))

    from pathlib import Path

    real = Path.read_text

    def deny(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(f"simulated unreadable file: {self}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)
    result = cli.invoke(
        "extract", "some-item-id", "--schema", str(locked), "--json", env=AUTH_ENV
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "bad_schema"


def test_a_non_utf8_schema_file_is_a_structured_usage_error(cli, tmp_path):
    # read_text raises UnicodeDecodeError, not OSError — it must exit
    # through the same structured path, never a raw traceback (#143).
    binary = tmp_path / "binary-schema.json"
    binary.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    result = cli.invoke(
        "extract", "some-item-id", "--schema", str(binary), "--json", env=AUTH_ENV
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "bad_schema"


# --- completion output discoverability (#34) ---


def summary_next_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.strip().startswith("next:"))


def test_extract_summary_advertises_store_dir_and_next_commands(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())

    fresh = cli.invoke("extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV)
    cached = cli.invoke("extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV)

    (extract_dir,) = extract_item_dirs(cli)
    # The cached-hit path serves the same summary (#34).
    for result in (fresh, cached):
        assert result.exit_code == 0, result.stdout
        assert (
            f"saved:    {extract_dir}/  (extract.json, evidence.json)"
            in result.stdout
        )
        line = summary_next_line(result.stdout)
        assert "ade history list" in line
        # The viewer hint names the extract item's OWN id (#170): that is
        # the only viewer that renders this extraction — the parse item's
        # viewer deliberately holds parse only.
        assert "ade view " in line and "--open" in line
        ref = line.split("ade view ", 1)[1].split()[0]
        assert extract_dir.name.startswith(ref)
        assert not parse_id.startswith(ref)


def test_extract_json_carries_the_contract_fields(cli, document, schema_file):
    parse_id = parse_doc(cli, document)

    payload = complete_extract(cli, parse_id, "--schema", str(schema_file))

    (extract_dir,) = extract_item_dirs(cli)
    assert payload["store_dir"] == str(extract_dir)
    assert payload["job_item_id"] == extract_dir.name
    assert payload["parse_job_item_id"] == parse_id
    assert payload["artifacts"] == ["extract.json", "evidence.json"]
    assert set(payload) == {
        "status", "run_id", "job_item_id", "parse_job_item_id", "environment",
        "version", "credits", "tier", "extraction", "fields", "ungroundable",
        "empty_fields", "schema_violation_error", "warnings", "evidence",
        "cached", "stored", "store_dir", "artifacts",
    }
    assert payload["environment"] == "production"
    # Clean run: the partial-success signals are present and null/empty,
    # so a scripter gates on them without probing (#118).
    assert payload["schema_violation_error"] is None
    assert payload["warnings"] == []
    # The result itself is on stdout, verbatim from the stored artifact —
    # never only in a file the caller has to find (F9).
    stored = json.loads((extract_dir / "extract.json").read_text())
    assert payload["extraction"] == stored["extraction"]


# --- partial extraction (#118): a reduced result is labeled, never silent ---

VIOLATION = (
    "'profit' is a required property.\nPlease read our documentation at "
    "https://docs.landing.ai/ade/ade-extract-schema-json for more details."
)
WARNING = {"type": "field_skipped", "message": "profit could not be extracted"}


def partial_result():
    """A completed (HTTP 200, status=completed) result that the server
    billed at the partial tier: strict=false and a schema field skipped."""
    return extract_result(
        schema_violation_error=VIOLATION, warnings=[WARNING]
    )


def test_partial_extraction_is_labeled_never_a_silent_success(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)

    payload = complete_extract(
        cli, parse_id, "--schema", str(schema_file), result=partial_result()
    )

    # Still a success — the run completed and was billed — but labeled.
    assert payload["status"] == "extracted"
    assert payload["schema_violation_error"] == VIOLATION
    assert payload["warnings"] == [WARNING]
    # meta.json denormalizes the state for listings; extract.json keeps
    # both fields verbatim (raw response, ground truth).
    (extract_dir,) = extract_item_dirs(cli)
    meta = json.loads((extract_dir / "meta.json").read_text())
    assert meta["schema_violation_error"] == VIOLATION
    assert meta["warnings"] == 1
    stored = json.loads((extract_dir / "extract.json").read_text())
    assert stored["schema_violation_error"] == VIOLATION
    assert stored["warnings"] == [WARNING]


def test_partial_extraction_summary_says_so_out_loud(cli, document, schema_file):
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job(partial_result()))

    result = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV
    )

    assert result.exit_code == 0, result.stdout
    # The whole message, never a first line or a count: extra lines hang
    # under the label at the value column.
    assert "partial:  'profit' is a required property." in result.stdout
    assert "\n            Please read our documentation" in result.stdout
    # Each warning's content, not "1 server warning(s)".
    assert "warnings: profit could not be extracted" in result.stdout


def test_partial_state_survives_the_cached_path_and_listings(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    complete_extract(
        cli, parse_id, "--schema", str(schema_file), result=partial_result()
    )

    # The free dedup path serves the same labeled summary (#34 posture).
    cached = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )
    assert cached.exit_code == 0, cached.stdout
    payload = json.loads(cached.stdout)
    assert payload["cached"] is True
    assert payload["schema_violation_error"] == VIOLATION
    assert payload["warnings"] == [WARNING]

    # history list shows the state without opening extract.json...
    listing = cli.invoke("history", "list", "--json")
    records = json.loads(listing.stdout)
    extract_record = next(r for r in records if r["kind"] == "extract")
    assert extract_record["schema_violation_error"] == VIOLATION
    assert extract_record["warnings"] == 1  # the denormalized count
    plain = cli.invoke("history", "list")
    assert "partial: 'profit' is a required property" in plain.stdout
    assert "warnings: 1 server warning(s)" in plain.stdout
    # ...and the sidebar read model carries both (data-only).
    sidebar = (cli.home / "history.js").read_text()
    assert "schema_violation_error" in sidebar
    assert '"warnings": 1' in sidebar


def test_markdown_extraction_next_hint_points_at_its_own_viewer(cli, tmp_path):
    # A bring-your-own-markdown item owns a viewer too (the markdown pane
    # alone, opening on the Extract tab) — the hint names its own id
    # (#170), same as the parse-backed form.
    md = tmp_path / "notes.md"
    md.write_text("# Notes\nTotal: €42\n")
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())

    result = cli.invoke(
        "extract", "--markdown", str(md), "--schema", json.dumps(SCHEMA), env=AUTH_ENV
    )

    assert result.exit_code == 0, result.stdout
    (extract_dir,) = extract_item_dirs(cli)
    line = summary_next_line(result.stdout)
    assert "ade history list" in line
    ref = line.split("ade view ", 1)[1].split()[0]
    assert extract_dir.name.startswith(ref)


def test_extract_summary_and_payload_name_the_server_run_never_job(
    cli, document, schema_file
):
    """#153, extract side: `run:` in the summary, run_id in the payload,
    "job" only inside "job item"."""
    parse_id = parse_doc(cli, document)
    payload = complete_extract(cli, parse_id, "--schema", str(schema_file))
    assert payload["run_id"] == "extract-0001"
    assert "job_id" not in payload

    # The cached re-run serves the same summary shape, human mode.
    human = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), env=AUTH_ENV
    )
    assert human.exit_code == 0
    assert "\n  run:      extract-0001" in human.stdout
    assert "job:" not in human.stdout


def test_empty_schema_blocks_extract_d_before_the_parse_first_phase(cli, document):
    """#154 must gate ahead of *both* billable steps: `extract -d` on a
    never-parsed document would otherwise run (and bill) the standalone
    parse before the extract submit ever validated the schema."""
    result = cli.invoke(
        "extract", "-d", str(document),
        "--schema", '{"type": "object", "properties": {}}',
        "--json", env=AUTH_ENV,
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "empty_schema"
    assert cli.transport.requests == []  # no parse-first, no extract, nothing
