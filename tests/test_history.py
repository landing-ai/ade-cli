"""``history`` — the read model over the job-item store, driven through
the CLI seam.

Every command here is zero API calls; states derive from tickets and
artifacts on disk. The fake transport stays unscripted except where a
parse/extract seeds the store. Every run re-scans ``jobs/`` and rewrites
``history.js``, so manual deletion heals and the sidebar read model never
goes stale.
"""

import json

import pytest

from extract_fixtures import completed_extract_job
from parse_fixtures import completed_job, job_payload

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}


def parse_file(cli, path, *extra_args, job_id="job-0001"):
    """Seed the store: run a real parse to completion through the seam."""
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_job(job_id=job_id))
    result = cli.invoke("parse", "-d", str(path), *extra_args, "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


def extract_item(cli, parse_item_id, schema_file, *, job_id="extract-0001"):
    """Seed a referencing extract item: extract takes the parse job item id."""
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_extract_job(job_id=job_id))
    result = cli.invoke(
        "extract", parse_item_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 fake invoice bytes")
    return path


@pytest.fixture
def schema_file(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"total": {"type": "string"}, "vendor": {"type": "string"}},
            }
        )
    )
    return path


def history_js(cli):
    return (cli.home / "history.js").read_text(encoding="utf-8")


def history_js_items(cli):
    text = history_js(cli)
    prefix = "window.__ADE_HISTORY__ = "
    assert text.startswith(prefix)
    payload = json.loads(text[len(prefix) :].rstrip().rstrip(";"))
    return payload["items"]


def test_history_js_lists_latest_submission_first(cli, document):
    """The sidebar renders history.js in file order; the newest run is what
    the user just did, so it leads. `history list` keeps oldest-first."""
    first = parse_file(cli, document)
    cli.clock.sleep(60)  # the second run submits later
    second = parse_file(cli, document, "--tier", "standard", job_id="job-0002")

    result = cli.invoke("history", "list", "--json")

    assert [r["job_item_id"] for r in json.loads(result.stdout)] == [first, second]
    assert [item["id"] for item in history_js_items(cli)] == [second, first]


def test_bare_history_defaults_to_list(cli, document):
    item_id = parse_file(cli, document)

    result = cli.invoke("history", "--json")

    assert result.exit_code == 0
    listed = cli.invoke("history", "list", "--json")
    assert json.loads(result.stdout) == json.loads(listed.stdout)
    assert json.loads(result.stdout)[0]["job_item_id"] == item_id


def test_history_list_empty_store(cli):
    result = cli.invoke("history", "list", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
    assert cli.transport.requests == []  # read model: zero API calls


def test_history_list_shows_a_parse_item_as_a_full_record(cli, document):
    item_id = parse_file(cli, document)
    seen_requests = len(cli.transport.requests)

    result = cli.invoke("history", "list", "--json")

    assert result.exit_code == 0
    (record,) = json.loads(result.stdout)
    assert record["job_item_id"] == item_id
    assert record["kind"] == "parse"
    assert record["state"] == "parsed"
    assert record["source"] == str(document.resolve())
    # Params verbatim: the exact dict identity was derived from.
    assert record["params"] == {
        "model": "dpt-3-pro-latest",
        "options": {},
        "tier": "priority",
    }
    assert record["job_id"] == "job-0001"
    assert record["submitted_at"] is not None
    assert record["completed_at"] is not None
    # Artifact index: everything a consumer can read from the store path.
    names = {a["name"] for a in record["artifacts"]}
    assert {"parse.json", "parse.md", "elements.json", "meta.json", "job.json"} <= names
    assert len(cli.transport.requests) == seen_requests  # zero API calls

    human = cli.invoke("history", "list")
    assert item_id in human.stdout
    assert "parse" in human.stdout
    assert "parsed" in human.stdout
    # The ENV column sits between state and params.
    assert "parsed      production" in human.stdout
    assert "dpt-3-pro-latest" in human.stdout


def test_referencing_extract_renders_as_an_indented_child_row(
    cli, document, schema_file
):
    parse_id = parse_file(cli, document)
    extract_id = extract_item(cli, parse_id, schema_file)

    result = cli.invoke("history", "list", "--json")
    records = {r["job_item_id"]: r for r in json.loads(result.stdout)}
    assert records[extract_id]["kind"] == "extract"
    assert records[extract_id]["state"] == "extracted"
    # Parent linkage: the extract references the parse item, never copies it.
    assert records[extract_id]["parse"]["job_item_id"] == parse_id
    assert records[extract_id]["parse"]["missing"] is False
    assert records[extract_id]["fields"] == ["total", "vendor"]

    human = cli.invoke("history", "list")
    lines = human.stdout.splitlines()
    (parse_line,) = [line for line in lines if line.startswith(parse_id)]
    (child_line,) = [line for line in lines if extract_id in line]
    # The referencing extract is indented beneath its parse row.
    assert child_line.startswith("  ")
    assert lines.index(child_line) == lines.index(parse_line) + 1


# A survey-style schema: many fields, names longer than any column can
# afford — the shape that blew up listings before the params cap (#144).
WIDE_FIELDS = ["member_info"] + [
    f"question_{i:02d}_in_the_past_6_months_how_many_times_did_you_do_this"
    for i in range(1, 39)
]


@pytest.fixture
def wide_schema_file(tmp_path):
    path = tmp_path / "wide-schema.json"
    path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {name: {"type": "string"} for name in WIDE_FIELDS},
            }
        )
    )
    return path


def test_wide_schema_extract_params_are_capped_in_plain_lines_and_sidebar(
    cli, document, wide_schema_file
):
    parse_id = parse_file(cli, document)
    extract_id = extract_item(cli, parse_id, wide_schema_file)

    human = cli.invoke("history", "list")
    (line,) = [ln for ln in human.stdout.splitlines() if extract_id in ln]
    # First fields named (clipped), the rest folded — never all 39.
    assert "member_info" in line
    assert "+36 more" in line
    assert "extract-latest" in line
    assert "question_04" not in line

    # The sidebar shares the same rendering, so it inherits the cap.
    (entry,) = [i for i in history_js_items(cli) if i["id"] == extract_id]
    assert "+36 more" in entry["params"]
    # --json keeps the full field list; the cap is display-only.
    listed = cli.invoke("history", "list", "--json")
    records = {r["job_item_id"]: r for r in json.loads(listed.stdout)}
    assert records[extract_id]["fields"] == WIDE_FIELDS


def test_wide_schema_table_truncates_params_never_the_identity_columns(
    cli, document, wide_schema_file
):
    parse_id = parse_file(cli, document)
    extract_id = extract_item(cli, parse_id, wide_schema_file)

    cli.stdout_tty = True
    result = cli.invoke("history", "list", env={"COLUMNS": "100"})

    assert result.exit_code == 0
    # Headers render whole — a crushed table showed "JOB IT…" (#144).
    assert "JOB ITEM" in result.stdout
    assert "SOURCE" in result.stdout
    # The ids survive whole and SOURCE keeps its floor; PARAMS is what
    # yields (its cell crops, the folded tail never renders).
    assert parse_id in result.stdout
    assert extract_id in result.stdout
    assert "invoice" in result.stdout
    assert "+36 more" not in result.stdout  # cropped before the fold
    assert "question_03" not in result.stdout


def test_history_states_derive_from_tickets_zero_api_calls(cli, tmp_path):
    pending_doc = tmp_path / "pending.pdf"
    pending_doc.write_bytes(b"%PDF pending bytes")
    cli.transport.respond(202, {"job_id": "job-pend"})
    submitted = cli.invoke(
        "parse", "-d", str(pending_doc), "--wait", "0", "--json", env=AUTH_ENV
    )
    assert submitted.exit_code == 3  # pending is a normal outcome

    failed_doc = tmp_path / "failed.pdf"
    failed_doc.write_bytes(b"%PDF failed bytes")
    cli.transport.respond(202, {"job_id": "job-fail"})
    cli.transport.respond(
        200, job_payload("failed", job_id="job-fail", failure_reason="boom")
    )
    failed = cli.invoke("parse", "-d", str(failed_doc), "--json", env=AUTH_ENV)
    assert failed.exit_code == 1
    seen_requests = len(cli.transport.requests)

    result = cli.invoke("history", "list", "--json")

    by_state = {r["state"]: r for r in json.loads(result.stdout)}
    assert by_state["pending"]["job_id"] == "job-pend"
    assert by_state["failed"]["job_id"] == "job-fail"
    assert len(cli.transport.requests) == seen_requests


def test_clear_a_parse_item_cascades_to_referencing_extracts_with_notice(
    cli, document, schema_file
):
    parse_id = parse_file(cli, document)
    extract_id = extract_item(cli, parse_id, schema_file)

    result = cli.invoke("history", "clear", parse_id, "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cleared"] == [parse_id, extract_id]
    assert payload["cascaded"] == [extract_id]  # the notice, machine-readable
    assert not (cli.home / "jobs" / parse_id).exists()
    assert not (cli.home / "jobs" / extract_id).exists()  # never a dangling ref
    assert json.loads(cli.invoke("history", "list", "--json").stdout) == []

    # And the human notice says so explicitly.
    reparse_id = parse_file(cli, document, job_id="job-0002")
    re_extract = extract_item(cli, reparse_id, schema_file, job_id="extract-0002")
    human = cli.invoke("history", "clear", reparse_id)
    assert human.exit_code == 0
    assert "1 dependent extract" in human.stdout
    assert re_extract[:8] in human.stdout


def test_clear_an_extract_item_leaves_its_parse_alone(cli, document, schema_file):
    parse_id = parse_file(cli, document)
    extract_id = extract_item(cli, parse_id, schema_file)

    result = cli.invoke("history", "clear", extract_id, "--json")

    payload = json.loads(result.stdout)
    assert payload["cleared"] == [extract_id]
    assert payload["cascaded"] == []
    assert (cli.home / "jobs" / parse_id).is_dir()


def test_clear_all_empties_the_store(cli, document, schema_file):
    parse_id = parse_file(cli, document)
    extract_item(cli, parse_id, schema_file)

    result = cli.invoke("history", "clear", "--all", "--json")

    assert result.exit_code == 0
    assert len(json.loads(result.stdout)["cleared"]) == 2
    assert json.loads(cli.invoke("history", "list", "--json").stdout) == []


def test_clear_needs_exactly_one_target(cli, document):
    item_id = parse_file(cli, document)

    neither = cli.invoke("history", "clear", "--json")
    both = cli.invoke("history", "clear", item_id, "--all", "--json")

    assert neither.exit_code == 2
    assert both.exit_code == 2
    assert (cli.home / "jobs" / item_id).is_dir()


def test_manual_folder_deletion_heals_on_the_next_scan(cli, tmp_path, document):
    import shutil

    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF other bytes")
    kept = parse_file(cli, document)
    removed = parse_file(cli, other, job_id="job-0002")

    shutil.rmtree(cli.home / "jobs" / removed)

    result = cli.invoke("history", "list", "--json")
    assert [r["job_item_id"] for r in json.loads(result.stdout)] == [kept]
    # history.js is rewritten from the same fresh scan.
    ids = {item["id"] for item in history_js_items(cli)}
    assert ids == {kept}


def test_orphaned_referencing_extract_degrades_to_parse_missing(
    cli, document, schema_file
):
    import shutil

    parse_id = parse_file(cli, document)
    extract_id = extract_item(cli, parse_id, schema_file)

    shutil.rmtree(cli.home / "jobs" / parse_id)  # manual, not history clear

    result = cli.invoke("history", "list", "--json")
    (record,) = json.loads(result.stdout)
    assert record["job_item_id"] == extract_id
    assert record["parse"]["job_item_id"] == parse_id
    assert record["parse"]["missing"] is True  # explicit, never dangling

    human = cli.invoke("history", "list")
    assert "parse missing" in human.stdout

    # The sidebar read model degrades the same way: no parent pointer to an
    # item that no longer exists — the orphan lists top-level.
    (item,) = history_js_items(cli)
    assert item["id"] == extract_id
    assert item["parent"] is None


def test_history_js_is_regenerated_from_a_fresh_scan_on_every_run(cli, document):
    item_id = parse_file(cli, document)

    cli.invoke("history", "list", "--json")
    (item,) = history_js_items(cli)
    assert item["id"] == item_id
    assert item["kind"] == "parse"
    assert item["state"] == "parsed"
    assert item["source_name"] == "invoice.pdf"
    assert item["parent"] is None
    assert item["viewer"] == "none"  # no view.html built yet
    assert item["href"] is None

    # A viewer artifact flips the status and href on the next scan.
    (cli.home / "jobs" / item_id / "view.html").write_text("<!doctype html>")
    cli.invoke("history", "list", "--json")
    (item,) = history_js_items(cli)
    assert item["viewer"] == "built"
    assert item["href"] == f"jobs/{item_id}/view.html"


def test_each_run_records_its_environment_end_to_end(cli, document, schema_file):
    # No --env / ADE_ENV: the default environment applies, recorded on the
    # ticket at claim, committed into meta.json, and projected into the
    # sidebar read model for both verbs.
    parse_id = parse_file(cli, document)
    extract_id = extract_item(cli, parse_id, schema_file)

    for item_id in (parse_id, extract_id):
        meta = json.loads((cli.home / "jobs" / item_id / "meta.json").read_text())
        assert meta["environment"] == "production"
        ticket = json.loads((cli.home / "jobs" / item_id / "job.json").read_text())
        assert ticket["environment"] == "production"
    cli.invoke("history", "list", "--json")
    envs = {item["id"]: item["environment"] for item in history_js_items(cli)}
    assert envs == {parse_id: "production", extract_id: "production"}


def test_the_env_flag_rides_into_history_js(cli, document):
    parse_file(cli, document, "--env", "staging")

    cli.invoke("history", "list", "--json")
    (item,) = history_js_items(cli)
    assert item["environment"] == "staging"


def test_the_ambient_ade_env_rides_into_history_js(cli, document):
    cli.transport.respond(202, {"job_id": "job-0001"})
    cli.transport.respond(200, completed_job(job_id="job-0001"))
    result = cli.invoke(
        "parse",
        "-d",
        str(document),
        "--json",
        env={**AUTH_ENV, "ADE_ENV": "eu"},
    )
    assert result.exit_code == 0, result.stdout

    cli.invoke("history", "list", "--json")
    (item,) = history_js_items(cli)
    assert item["environment"] == "eu"


def test_an_endpoint_override_never_puts_the_url_in_history_js(cli, document):
    # ADE_ENDPOINT overrides the URL alone; the item still records the
    # resolved environment name — the URL itself is a value and never
    # rides into the read model.
    cli.transport.respond(202, {"job_id": "job-0001"})
    cli.transport.respond(200, completed_job(job_id="job-0001"))
    result = cli.invoke(
        "parse",
        "-d",
        str(document),
        "--json",
        env={**AUTH_ENV, "ADE_ENDPOINT": "https://ade.internal.example.com"},
    )
    assert result.exit_code == 0, result.stdout

    cli.invoke("history", "list", "--json")
    (item,) = history_js_items(cli)
    assert item["environment"] == "production"
    assert "internal.example.com" not in history_js(cli)


def test_items_from_before_the_environment_field_render_without_it(cli, document):
    item_id = parse_file(cli, document)
    # Strip the field the way a pre-environment store would look.
    meta_path = cli.home / "jobs" / item_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    del meta["environment"]
    meta_path.write_text(json.dumps(meta))
    ticket_path = cli.home / "jobs" / item_id / "job.json"
    ticket = json.loads(ticket_path.read_text())
    ticket.pop("environment", None)
    ticket_path.write_text(json.dumps(ticket))

    cli.invoke("history", "list", "--json")

    (item,) = history_js_items(cli)
    assert item["environment"] is None


def test_history_js_payload_is_injection_safe(cli):
    # A store-controlled string carrying "</script>" must never be able to
    # close the carrier script tag; the serializer escapes "</" everywhere.
    url = "https://x.test/</script><script>alert(1)</script>"
    cli.transport.respond(202, {"job_id": "job-urly"})
    cli.transport.respond(200, completed_job(job_id="job-urly"))
    result = cli.invoke("parse", "--document-url", url, "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout

    cli.invoke("history", "list", "--json")

    text = history_js(cli)
    assert "</script" not in text
    assert "<\\/script" in text
    # The escaped form decodes back to the exact source string.
    (item,) = history_js_items(cli)
    assert item["source"] == url


def test_unknown_and_ambiguous_ids_error_with_candidates(cli, tmp_path):
    # Seeded directly so the shared prefix is deterministic — the scan sees
    # any directory holding metadata, exactly like a real item.
    twins = ("abcd1234aaaaaaaa", "abcd1234bbbbbbbb")
    for item_id in twins:
        d = cli.home / "jobs" / item_id
        d.mkdir(parents=True)
        (d / "meta.json").write_text(
            json.dumps({"kind": "parse", "state": "parsed", "source": "x.pdf"})
        )

    unknown = cli.invoke("history", "clear", "zzzzzzzz", "--json")
    assert unknown.exit_code == 1
    payload = json.loads(unknown.stdout)
    assert payload["error"] == "unknown_id"
    assert "history list" in payload["message"]

    ambiguous = cli.invoke("history", "clear", "abcd1234", "--json")
    assert ambiguous.exit_code == 2
    payload = json.loads(ambiguous.stdout)
    assert payload["error"] == "ambiguous_id"
    assert set(payload["candidates"]) == set(twins)

    # A path is not a resolution rule any more: the file exists, but only
    # ids resolve. The remediation is history list or the convenience verbs.
    existing = tmp_path / "real.pdf"
    existing.write_bytes(b"%PDF real")
    by_path = cli.invoke("history", "clear", str(existing), "--json")
    assert by_path.exit_code == 1
    assert json.loads(by_path.stdout)["error"] == "unknown_id"
