"""``extract -d FILE`` — the document-path input form (issue #60, decision
10 as revised: no embedded parse).

The path form is a convenience verb over the same guarantee: reuse the
latest completed parse job of this path+content (logged, referenced via
``parse/ref.json``) or, when none exists, run a **standalone parse job
first** — a normal top-level parse item, exactly as if the user had run
``parse -d`` — then the extract referencing it. Every parse the CLI runs
is reusable, so repeated ``extract -d`` on a never-parsed document bills
the parse exactly once. Every test drives the CLI seam; billing-visible
behavior (submits) is asserted on the fake transport.
"""

import json

import pytest

from ade_cli import store as jobstore

from extract_fixtures import SCHEMA, completed_extract_job, extract_result
from parse_fixtures import (
    JOB_ID,
    MARKDOWN,
    MODEL_VERSION,
    completed_job,
    job_payload,
    parse_response,
)

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}
DOCUMENT_BYTES = b"%PDF-1.4 fake invoice bytes"

OTHER_SCHEMA = {"type": "object", "properties": {"date": {"type": "string"}}}

# The parse-first phase always runs bare `parse -d` params: default model,
# default options, default (priority) tier — whatever the extract's flags.
DEFAULT_PARSE_PARAMS = {"model": "dpt-3-pro-latest", "options": {}, "tier": "priority"}


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(DOCUMENT_BYTES)
    return path


@pytest.fixture
def schema_file(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(SCHEMA))
    return path


def posts(cli, path):
    return [
        r
        for r in cli.transport.requests
        if r.method == "POST" and r.url.path == path
    ]


def parse_posts(cli):
    return posts(cli, "/v2/parse/jobs")


def extract_posts(cli):
    return posts(cli, "/v2/extract/jobs")


def parse_doc(cli, document, *, job_id=JOB_ID, tier=None, response=None):
    """Seed a completed parse job item via `parse -d`; returns its item id."""
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_job(response, job_id=job_id))
    args = ["parse", "-d", str(document)]
    if tier:
        args += ["--tier", tier]
    result = cli.invoke(*args, "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


def extract_doc(cli, document, *args, extract_job_id="extract-0001", result=None):
    """Run ``extract -d`` to completion; the caller scripts any parse
    responses the run needs before calling."""
    cli.transport.respond(202, {"job_id": extract_job_id})
    cli.transport.respond(200, completed_extract_job(result, job_id=extract_job_id))
    invoked = cli.invoke("extract", "-d", str(document), *args, "--json", env=AUTH_ENV)
    assert invoked.exit_code == 0, invoked.stdout
    return json.loads(invoked.stdout)


def fresh_extract(cli, document, *args, **kwargs):
    """extract -d with no reusable parse: scripts the parse-first job's
    submit + poll ahead of the extract's."""
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    return extract_doc(cli, document, *args, **kwargs)


def item_dirs(cli):
    return sorted(d for d in (cli.home / "jobs").iterdir() if d.is_dir())


def default_parse_item_id(document):
    return jobstore.derive_id(
        "parse",
        "production",
        jobstore.local_identity(document, DOCUMENT_BYTES),
        DEFAULT_PARSE_PARAMS,
    )


# --- reuse: the latest completed parse of this path+content ---


def test_extract_d_reuses_the_latest_parse_and_references_it(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)

    payload = extract_doc(cli, document, "--schema", str(schema_file))

    # No parse billed: one parse submit total, and the reuse is explicit.
    assert len(parse_posts(cli)) == 1
    assert payload["parse_job_item_id"] == parse_id
    assert payload["reused_parse"]["job_item_id"] == parse_id
    assert payload["reused_parse"]["model_version"] == MODEL_VERSION

    # Referenced like the JOB_ID form — the same identity formula, so the
    # two spellings resolve to the same extract job item.
    assert payload["job_item_id"] == jobstore.derive_id(
        "extract",
        "production",
        jobstore.local_identity(document, DOCUMENT_BYTES),
        {
            "schema": SCHEMA,
            "model": "extract-latest",
            "options": {},
            "parse_job_item_id": parse_id,
        },
    )
    item_dir = cli.home / "jobs" / payload["job_item_id"]
    ref = json.loads((item_dir / "parse/ref.json").read_text())
    # No direct flag: this extract REUSED an existing parse (the flag marks
    # only parses an extract invocation created).
    assert ref == {"job_item_id": parse_id, "parse_job_id": JOB_ID}
    assert not (item_dir / "parse" / "parse.json").exists()  # never copied

    # The extract POST sent the reused parse's markdown (doc_id trailer and
    # all) — that is what the spans index.
    (submit,) = extract_posts(cli)
    assert json.loads(submit.content)["markdown"] == MARKDOWN


def test_reuse_is_logged_in_the_human_summary(cli, document, schema_file):
    parse_id = parse_doc(cli, document)
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())

    result = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file), env=AUTH_ENV
    )

    assert result.exit_code == 0, result.stdout
    assert f"reused parse job item {parse_id}" in result.stdout
    assert MODEL_VERSION in result.stdout  # model …
    assert "completed" in result.stdout  # … and completion time, both logged


def test_reuse_scan_picks_the_newest_completed_across_params_variants(
    cli, document, schema_file
):
    older = parse_doc(cli, document)
    cli.clock.sleep(60)  # the variant completes later
    newer = parse_doc(cli, document, job_id="job-0002", tier="standard")
    assert older != newer

    payload = extract_doc(cli, document, "--schema", str(schema_file))

    assert payload["parse_job_item_id"] == newer
    ref = json.loads(
        (cli.home / "jobs" / payload["job_item_id"] / "parse/ref.json").read_text()
    )
    assert ref["job_item_id"] == newer


def test_extract_d_and_extract_job_id_are_the_same_invocation(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    first = extract_doc(cli, document, "--schema", str(schema_file))
    seen = len(cli.transport.requests)

    again = cli.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )

    assert again.exit_code == 0
    assert len(cli.transport.requests) == seen  # served from disk
    payload = json.loads(again.stdout)
    assert payload["job_item_id"] == first["job_item_id"]
    assert payload["cached"] is True


def test_stale_reused_extraction_reextracts_in_place_after_forced_reparse(
    cli, document, schema_file
):
    parse_id = parse_doc(cli, document)
    first = extract_doc(cli, document, "--schema", str(schema_file))

    # A forced re-parse replaces the parse generation the extraction ran
    # against — the reuse scan still finds the same (newest) parse item, but
    # the recorded parse_job_id no longer matches: stale, re-extract in place.
    new_markdown = "# Reparsed\n\nTotal: €99\n\n<!-- doc_id=srv-doc-77aa00 -->\n"
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(
        200, completed_job(parse_response(markdown=new_markdown, job_id="job-0002"))
    )
    forced = cli.invoke("parse", "-d", str(document), "--force", env=AUTH_ENV)
    assert forced.exit_code == 0

    payload = extract_doc(
        cli,
        document,
        "--schema",
        str(schema_file),
        extract_job_id="extract-0002",
        result=extract_result(markdown=new_markdown, job_id="extract-0002"),
    )

    assert payload["job_item_id"] == first["job_item_id"]  # same item, re-run
    assert payload["cached"] is False
    assert payload["parse_job_item_id"] == parse_id
    assert len(extract_posts(cli)) == 2
    assert json.loads(extract_posts(cli)[-1].content)["markdown"] == new_markdown
    ref = json.loads(
        (cli.home / "jobs" / first["job_item_id"] / "parse/ref.json").read_text()
    )
    assert ref["parse_job_id"] == "job-0002"


# --- parse-first: no reusable parse — run a standalone parse job, then
# --- extract referencing it (decision 10, revised) ---


def test_fresh_extract_d_runs_a_standalone_parse_then_references_it(
    cli, document, schema_file
):
    payload = fresh_extract(cli, document, "--schema", str(schema_file))

    # Two billable jobs, one invocation, both itemised.
    assert len(parse_posts(cli)) == 1
    assert len(extract_posts(cli)) == 1
    parse_id = default_parse_item_id(document)
    assert payload["parse_job_item_id"] == parse_id
    assert payload["parsed_first"] == {
        "job_item_id": parse_id,
        "run_id": JOB_ID,
        "version": MODEL_VERSION,
        "credits": 2.5,
        "tier": "priority",
    }

    # The parse is a normal top-level parse item — exactly what `parse -d`
    # would have minted — and the extract references it like any other.
    assert sorted(d.name for d in item_dirs(cli)) == sorted(
        [parse_id, payload["job_item_id"]]
    )
    parse_dir = cli.home / "jobs" / parse_id
    for name in ("job.json", "meta.json", "parse.json", "parse.md", "elements.json"):
        assert (parse_dir / name).exists(), name
    assert (parse_dir / "parse.md").read_text() == MARKDOWN
    item_dir = cli.home / "jobs" / payload["job_item_id"]
    ref = json.loads((item_dir / "parse/ref.json").read_text())
    # direct: true = this extract invocation created the parse (provenance,
    # not ownership — the parse is a normal standalone, reusable item).
    assert ref == {"job_item_id": parse_id, "parse_job_id": JOB_ID, "direct": True}
    assert not (item_dir / "parse" / "parse.json").exists()  # ref, never a copy

    # Identity carries the parse linkage — every parse-backed extraction does.
    assert payload["job_item_id"] == jobstore.derive_id(
        "extract",
        "production",
        jobstore.local_identity(document, DOCUMENT_BYTES),
        {
            "schema": SCHEMA,
            "model": "extract-latest",
            "options": {},
            "parse_job_item_id": parse_id,
        },
    )

    # The extract ran against the fresh parse's markdown; evidence joins
    # against its grounding.
    (submit,) = extract_posts(cli)
    assert json.loads(submit.content)["markdown"] == MARKDOWN
    assert payload["evidence"]["kind"] == "grounded"

    # Both items are first-class history rows, the extract under its parse.
    listed = cli.invoke("history", "list", "--json", env=AUTH_ENV)
    records = {r["job_item_id"]: r for r in json.loads(listed.stdout)}
    assert records[parse_id]["kind"] == "parse"
    assert records[parse_id]["state"] == "parsed"
    assert records[payload["job_item_id"]]["parse"]["job_item_id"] == parse_id


def test_parse_first_always_runs_bare_parse_defaults(cli, document, schema_file):
    # --tier governs the extract job; the parse-first job runs bare
    # `parse -d` params, so it lands on the exact item a later plain
    # `parse -d` would dedup against.
    payload = fresh_extract(
        cli, document, "--schema", str(schema_file), "--tier", "standard"
    )

    assert payload["parsed_first"]["job_item_id"] == default_parse_item_id(document)
    (parse_submit,) = parse_posts(cli)
    assert b"priority" in parse_submit.content  # the parse lane, multipart field
    (extract_submit,) = extract_posts(cli)
    assert json.loads(extract_submit.content)["service_tier"] == "standard"

    # A later bare `parse -d` is the same invocation: served from disk.
    seen = len(cli.transport.requests)
    reparse = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    assert reparse.exit_code == 0
    assert json.loads(reparse.stdout)["cached"] is True
    assert len(cli.transport.requests) == seen


def test_fresh_summary_itemises_both_bills(cli, document, schema_file):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())

    result = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file), env=AUTH_ENV
    )

    assert result.exit_code == 0, result.stdout
    parse_id = default_parse_item_id(document)
    # Both bills where the money is: the parse's job item, model, and
    # credits itemised next to the extract's own credits line.
    assert f"parsed first — job item {parse_id}" in result.stdout
    assert "2.5" in result.stdout  # the parse bill
    assert "1.0" in result.stdout or "1 (" in result.stdout  # the extract bill
    # The convenience verb says up front that it is about to bill a parse.
    assert "no reusable parse" in result.stderr


def test_repeated_fresh_extract_d_bills_the_parse_exactly_once(
    cli, document, schema_file
):
    first = fresh_extract(cli, document, "--schema", str(schema_file))
    seen = len(cli.transport.requests)

    again = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file),
        "--json", env=AUTH_ENV,
    )

    # The parse-first job is a reusable item like any other: the second run
    # reuses it and dedups the extraction — zero API calls.
    assert again.exit_code == 0
    assert len(cli.transport.requests) == seen
    payload = json.loads(again.stdout)
    assert payload["cached"] is True
    assert payload["job_item_id"] == first["job_item_id"]
    assert payload["reused_parse"]["job_item_id"] == first["parse_job_item_id"]

    human = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file), env=AUTH_ENV
    )
    assert human.exit_code == 0
    assert "already extracted" in human.stdout
    assert "--force" in human.stdout


def test_a_new_schema_reuses_the_parse_first_item(cli, document, schema_file):
    first = fresh_extract(cli, document, "--schema", str(schema_file))

    payload = extract_doc(
        cli,
        document,
        "--schema",
        json.dumps(OTHER_SCHEMA),
        extract_job_id="extract-0002",
    )

    # One parse bill total across both invocations; sibling extract items.
    assert len(parse_posts(cli)) == 1
    assert len(extract_posts(cli)) == 2
    assert payload["job_item_id"] != first["job_item_id"]
    assert payload["reused_parse"]["job_item_id"] == first["parse_job_item_id"]
    assert len(item_dirs(cli)) == 3  # one parse item + two extract items


def test_force_reextracts_without_reparsing(cli, document, schema_file):
    first = fresh_extract(cli, document, "--schema", str(schema_file))

    payload = extract_doc(
        cli, document, "--schema", str(schema_file), "--force",
        extract_job_id="extract-0002",
    )

    # --force is extract's consent to re-bill the extraction; the
    # referenced parse is untouched (its own --force lives on `parse`).
    assert payload["job_item_id"] == first["job_item_id"]
    assert payload["run_id"] == "extract-0002"
    assert len(parse_posts(cli)) == 1
    assert len(extract_posts(cli)) == 2


def test_interrupted_parse_phase_leaves_a_resumable_parse_item(
    cli, document, schema_file
):
    cli.transport.respond(202, {"job_id": JOB_ID})
    first = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file),
        "--wait", "0", "--json", env=AUTH_ENV,
    )

    assert first.exit_code == 3  # pending is a normal outcome
    payload = json.loads(first.stdout)
    assert payload["status"] == "pending"
    assert payload["run_id"] == JOB_ID
    parse_id = default_parse_item_id(document)
    assert payload["job_item_id"] == parse_id  # the item awaiting work

    # The pending parse is a standalone item: visible in history, ticket
    # holding the recorded job.
    listed = cli.invoke("history", "list", "--json", env=AUTH_ENV)
    (record,) = json.loads(listed.stdout)
    assert record["job_item_id"] == parse_id
    assert record["kind"] == "parse"
    assert record["state"] == "pending"
    ticket = json.loads(
        (cli.home / "jobs" / parse_id / "job.json").read_text()
    )
    assert ticket["job_id"] == JOB_ID

    # The re-run resumes the recorded parse job (never resubmits), then
    # extracts — exactly one parse submit across both runs.
    cli.transport.respond(200, completed_job())
    done = extract_doc(cli, document, "--schema", str(schema_file))
    assert done["parse_job_item_id"] == parse_id
    assert len(parse_posts(cli)) == 1
    assert len(extract_posts(cli)) == 1


def test_plain_parse_resumes_the_parse_first_pending_job_too(
    cli, document, schema_file
):
    # The parse-first item IS a `parse -d` item, so the plain verb is an
    # equally valid recovery gesture for its pending job.
    cli.transport.respond(202, {"job_id": JOB_ID})
    pending = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file),
        "--wait", "0", "--json", env=AUTH_ENV,
    )
    assert pending.exit_code == 3

    cli.transport.respond(200, completed_job())
    resumed = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert resumed.exit_code == 0
    assert json.loads(resumed.stdout)["run_id"] == JOB_ID
    assert len(parse_posts(cli)) == 1  # joined, never resubmitted


def test_interrupt_during_the_extract_phase_never_reparses_on_resume(
    cli, document, schema_file
):
    # Ctrl-C lands while the extract job polls: the parse item is already
    # published, the extract's claim ticket holds its job id.
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, job_payload("processing", job_id="extract-0001"))
    cli.clock.interrupt_sleep_at = 0  # Ctrl-C on the first poll backoff
    first = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file),
        "--json", env=AUTH_ENV,
    )
    assert first.exit_code == 3
    payload = json.loads(first.stdout)
    assert payload["run_id"] == "extract-0001"

    # The re-run reuses the published parse item and rejoins the recorded
    # extract job — one submit each, across both runs.
    cli.clock.interrupt_sleep_at = None
    cli.transport.respond(200, completed_extract_job())
    done = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file),
        "--json", env=AUTH_ENV,
    )
    assert done.exit_code == 0
    assert json.loads(done.stdout)["reused_parse"]["job_item_id"] == (
        default_parse_item_id(document)
    )
    assert len(parse_posts(cli)) == 1
    assert len(extract_posts(cli)) == 1


def test_clearing_the_parse_first_item_cascades_to_its_extract(
    cli, document, schema_file
):
    payload = fresh_extract(cli, document, "--schema", str(schema_file))
    parse_id = payload["parse_job_item_id"]

    cleared = cli.invoke("history", "clear", parse_id, "--json", env=AUTH_ENV)

    assert cleared.exit_code == 0
    assert json.loads(cleared.stdout)["cleared"] == [
        parse_id,
        payload["job_item_id"],
    ]
    assert item_dirs(cli) == []  # no dangling refs, ever


# --- input exclusivity ---


def test_document_is_exclusive_with_the_other_inputs(cli, document, schema_file, tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("hello")

    with_job_id = cli.invoke(
        "extract", "some-item-id", "-d", str(document),
        "--schema", str(schema_file), env=AUTH_ENV,
    )
    assert with_job_id.exit_code == 2

    with_markdown = cli.invoke(
        "extract", "-d", str(document), "--markdown", str(md),
        "--schema", str(schema_file), env=AUTH_ENV,
    )
    assert with_markdown.exit_code == 2
