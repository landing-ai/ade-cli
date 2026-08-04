import json

import pytest

from ade_cli import store as jobstore

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

DOC_BYTES = b"%PDF-1.4 fake invoice bytes"
DEFAULT_PARAMS = {"model": "dpt-3-pro-latest", "options": {}, "tier": "priority"}


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(DOC_BYTES)
    return path


def item_dir(cli, document, params=DEFAULT_PARAMS, content=DOC_BYTES):
    item_id = jobstore.derive_id(
        "parse", "production", jobstore.local_identity(document, content), params
    )
    return cli.home / "jobs" / item_id


def test_parse_submits_polls_and_persists_artifacts(cli, document):
    data = parse_response()
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, job_payload("pending"))
    cli.transport.respond(200, completed_job(data))

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 0
    d = item_dir(cli, document)
    assert json.loads((d / "parse.json").read_text()) == data
    assert (d / "parse.md").read_text() == MARKDOWN  # doc_id trailer kept
    assert json.loads((d / "job.json").read_text())["job_id"] == JOB_ID
    meta = json.loads((d / "meta.json").read_text())
    assert meta["state"] == "parsed"
    assert meta["kind"] == "parse"
    # Params verbatim — the exact dict identity was derived from (tier
    # included: it is part of how this run was billed).
    assert meta["params"] == DEFAULT_PARAMS
    # The identity components, reusable by extract (same document, new verb).
    assert set(meta["identity"]) == {"source_hash", "content_hash"}
    assert "€42" in (d / "parse.json").read_text()  # raw kept unescaped

    # summary: job_id, resolved model version, credits, tier
    assert JOB_ID in result.stdout
    assert MODEL_VERSION in result.stdout
    assert "2.5" in result.stdout
    assert "priority" in result.stdout


def test_submit_follows_the_async_wire_contract(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    submit, poll = cli.transport.requests
    assert submit.method == "POST"
    assert submit.url.path == "/v2/parse/jobs"  # async route, bare /v2/*
    assert submit.headers["authorization"] == f"Bearer {KEY}"
    assert submit.headers["content-type"].startswith("multipart/form-data")
    assert b'name="service_tier"' in submit.content  # always sent
    assert b'name="priority"' not in submit.content  # deprecated alias, never
    assert DOC_BYTES in submit.content
    assert poll.method == "GET"
    assert poll.url.path == f"/v2/parse/jobs/{JOB_ID}"
    assert poll.headers["authorization"] == f"Bearer {KEY}"


def test_tier_pages_model_pass_through(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    cli.invoke(
        "parse", "-d", str(document),
        "--tier", "standard", "--model", "dpt-3-pro-20260515",
        "--pages", "1-3,6",
        env=AUTH_ENV,
    )

    body = cli.transport.requests[0].content
    assert b"standard" in body
    assert b"dpt-3-pro-20260515" in body
    options = json.loads(_multipart_field(body, "options"))
    assert options == {"pages": [1, 2, 3, 6]}  # wire contract: 1-indexed integer array


def test_options_json_passes_through_to_the_wire(cli, document):
    # Full ParseOptions pass-through: sent exactly as given, never
    # interpreted — unknown keys are the server's 422 to raise, not ours.
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    cli.invoke(
        "parse", "-d", str(document),
        "--options",
        '{"atomic_grounding": false, "inline_markdown": true, '
        '"blocks": {"table": {"format": "markdown"}}}',
        env=AUTH_ENV,
    )

    options = json.loads(_multipart_field(cli.transport.requests[0].content, "options"))
    assert options == {
        "atomic_grounding": False,
        "inline_markdown": True,
        "blocks": {"table": {"format": "markdown"}},
    }


def test_options_and_pages_merge_when_disjoint(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    cli.invoke(
        "parse", "-d", str(document),
        "--pages", "2-3", "--options", '{"inline_markdown": true}',
        env=AUTH_ENV,
    )

    options = json.loads(_multipart_field(cli.transport.requests[0].content, "options"))
    assert options == {"inline_markdown": True, "pages": [2, 3]}


def test_pages_in_both_flags_is_a_usage_error(cli, document):
    # The documented conflict rule: the page selection is given once.
    # Precedence would silently bill a page set the user didn't intend.
    result = cli.invoke(
        "parse", "-d", str(document),
        "--pages", "1", "--options", '{"pages": [2]}',
        env=AUTH_ENV,
    )

    assert result.exit_code == 2
    assert "pages" in result.stdout.lower()
    assert not cli.transport.requests  # rejected before any submit


# not JSON at all, a JSON array, a JSON string — options must be an object
@pytest.mark.parametrize("bad", ["not-json", "[1, 2]", '"pages"'])
def test_invalid_options_json_is_a_usage_error(cli, document, bad):
    result = cli.invoke(
        "parse", "-d", str(document), "--options", bad, env=AUTH_ENV
    )

    assert result.exit_code == 2
    assert not cli.transport.requests


def test_pages_flag_and_options_pages_are_the_same_invocation(cli, document):
    # --pages is a spelling convenience, not a separate identity component:
    # the same pages array via --options resolves to the same job item and
    # is served from disk free.
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    first = cli.invoke(
        "parse", "-d", str(document), "--pages", "1,2", "--json", env=AUTH_ENV
    )

    again = cli.invoke(
        "parse", "-d", str(document), "--options", '{"pages": [1, 2]}', "--json",
        env=AUTH_ENV,
    )

    assert json.loads(first.stdout)["job_item_id"] == json.loads(again.stdout)["job_item_id"]
    assert json.loads(again.stdout)["cached"] is True
    assert len(cli.transport.requests) == 2  # one submit + one poll, ever


def test_options_variants_are_sibling_items_both_in_history(cli, document):
    for job_id in ("job-0001", "job-0002"):
        cli.transport.respond(202, {"job_id": job_id})
        cli.transport.respond(200, completed_job(job_id=job_id))

    plain = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    variant = cli.invoke(
        "parse", "-d", str(document),
        "--options", '{"atomic_grounding": false}', "--json",
        env=AUTH_ENV,
    )

    ids = {json.loads(r.stdout)["job_item_id"] for r in (plain, variant)}
    assert len(ids) == 2  # sibling variant, nothing replaced
    listed = cli.invoke("history", "list", "--json")
    assert {r["job_item_id"] for r in json.loads(listed.stdout)} == ids


def test_parse_help_teaches_the_options_surface(cli):
    # --help is where an agent discovers what --options accepts: every
    # documented ParseOptions key, its default posture, and an example —
    # without needing the OpenAPI spec at hand.
    result = cli.invoke("parse", "--help")

    assert result.exit_code == 0
    for key in (
        "pages",
        "atomic_grounding",
        "inline_markdown",
        "markdown",  # blocks.<type>.markdown
        "format",  # blocks.table.format
        "password",
    ):
        assert key in result.stdout
    for block_type in (
        "text", "table", "figure", "marginalia",
        "attestation", "logo", "scan_code", "card",
    ):
        assert block_type in result.stdout
    assert "default" in result.stdout  # defaults are stated, not implied
    assert "422" in result.stdout  # unknown keys: the server rejects, loudly


def test_rejected_option_surfaces_the_servers_code_and_message(cli, document):
    # Retired keys (dpi, grounding, blocks.<type>.caption) are the server's
    # 422 to reject; the CLI surfaces the {code, message} envelope naming
    # the key instead of a blank or raw-JSON error.
    cli.transport.respond(
        422, {"code": "validation_error", "message": "Unknown option key: 'dpi'"}
    )

    result = cli.invoke(
        "parse", "-d", str(document), "--options", '{"dpi": 300}', "--json",
        env=AUTH_ENV,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "http"
    assert payload["status_code"] == 422
    assert payload["code"] == "validation_error"
    assert "dpi" in payload["message"]
    # The deterministic 4xx released the claim: a corrected invocation is a
    # fresh guarantee, not a wait on a dead claim ticket.
    item = next((cli.home / "jobs").iterdir())
    assert not (item / "job.json").exists()

    # The human rendering names the code and key too, never a bare status.
    cli.transport.respond(
        422, {"code": "validation_error", "message": "Unknown option key: 'dpi'"}
    )
    human = cli.invoke(
        "parse", "-d", str(document), "--options", '{"dpi": 300}', env=AUTH_ENV
    )
    assert human.exit_code == 1
    assert "validation_error" in human.stdout
    assert "dpi" in human.stdout


# non-numeric, reversed range, 0 (pages are 1-indexed)
@pytest.mark.parametrize("spec", ["one-3", "5-3", "0-2"])
def test_invalid_pages_spec_is_a_usage_error(cli, document, spec):
    result = cli.invoke(
        "parse", "-d", str(document), "--pages", spec, env=AUTH_ENV
    )

    assert result.exit_code == 2
    assert not cli.transport.requests  # rejected before any submit


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    assert marker in body, f"multipart field {name} missing"
    after = body.split(marker, 1)[1]
    return after.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0].decode()


def test_same_bytes_at_a_different_path_is_a_sibling_job_item(cli, tmp_path, document):
    # Identity is the invocation: moving or copying a file changes its
    # source-path component, so the copy is a new job item and a new parse
    # bills (accepted consequence — the proposal's cross-path dedup is gone).
    copy = tmp_path / "renamed" / "other-name.pdf"
    copy.parent.mkdir()
    copy.write_bytes(DOC_BYTES)
    for _ in range(2):
        cli.transport.respond(202, {"job_id": JOB_ID})
        cli.transport.respond(200, completed_job())

    first = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    second = cli.invoke("parse", "-d", str(copy), "--json", env=AUTH_ENV)

    assert json.loads(first.stdout)["job_item_id"] != json.loads(second.stdout)["job_item_id"]
    assert len(list((cli.home / "jobs").iterdir())) == 2


def test_different_params_on_the_same_file_are_sibling_variants(cli, document):
    # Params live inside identity: variants coexist, nothing is replaced.
    for _ in range(2):
        cli.transport.respond(202, {"job_id": JOB_ID})
        cli.transport.respond(200, completed_job())

    priority = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    standard = cli.invoke(
        "parse", "-d", str(document), "--tier", "standard", "--json", env=AUTH_ENV
    )

    assert json.loads(priority.stdout)["job_item_id"] != json.loads(standard.stdout)["job_item_id"]
    assert len(list((cli.home / "jobs").iterdir())) == 2
    # And the exact same invocation is served from disk, no third submit.
    cached = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    assert json.loads(cached.stdout)["cached"] is True
    assert len(cli.transport.requests) == 4  # two submits + two polls only


def test_failed_pages_and_206_surface_in_summary_and_metadata(cli, document):
    data = parse_response(page_count=5, failed_pages=[1, 3])
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(206, completed_job(data))

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 0  # partial success is surfaced, not an error
    assert "2 failed: 1, 3" in result.stdout
    meta = json.loads((item_dir(cli, document) / "meta.json").read_text())
    assert meta["failed_pages"] == [1, 3]


def test_document_url_submits_and_keys_the_store_by_url_hash(cli):
    url = "https://example.com/statement.pdf"
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "--document-url", url, "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    item_id = json.loads(result.stdout)["job_item_id"]
    # URL sources have no content component — the CLI never sees the bytes.
    assert item_id == jobstore.derive_id(
        "parse", "production", jobstore.url_identity(url), DEFAULT_PARAMS
    )
    assert url.encode() in cli.transport.requests[0].content
    assert (cli.home / "jobs" / item_id / "parse.json").exists()
    meta = json.loads((cli.home / "jobs" / item_id / "meta.json").read_text())
    assert set(meta["identity"]) == {"url_hash"}


def test_json_payload_carries_the_contract_fields(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    payload = json.loads(result.stdout)
    assert payload["status"] == "parsed"
    assert payload["run_id"] == JOB_ID
    assert payload["job_item_id"] == item_dir(cli, document).name
    assert payload["version"] == MODEL_VERSION
    assert payload["credits"] == 2.5
    # #34: additive keys only — where artifacts land, and exactly which.
    assert payload["store_dir"] == str(item_dir(cli, document))
    assert payload["artifacts"] == ["parse.json", "parse.md", "elements.json"]
    assert set(payload) == {
        "status", "run_id", "job_item_id", "environment", "version",
        "credits", "tier", "page_count", "failed_pages", "cached", "stored",
        "store_dir", "artifacts",
    }
    assert payload["environment"] == "production"


def test_include_carries_bulk_artifacts_on_stdout(cli, document):
    """F9: the result never *requires* reading a file out of the store —
    the bulk artifacts come to stdout on request."""
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    result = cli.invoke(
        "parse", "-d", str(document), "--include", "markdown",
        "--include", "elements", "--json", env=AUTH_ENV,
    )

    payload = json.loads(result.stdout)
    assert payload["markdown"] == MARKDOWN
    assert payload["markdown"] == (item_dir(cli, document) / "parse.md").read_text()
    stored = json.loads((item_dir(cli, document) / "elements.json").read_text())
    assert payload["elements"] == stored["elements"]


def test_include_is_opt_in_and_serves_the_cached_path_too(cli, document):
    """Off by default (a long document's markdown is megabytes), and the
    free cached hit answers exactly like the billed run."""
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    fresh = json.loads(cli.invoke(
        "parse", "-d", str(document), "--include", "markdown", "--json", env=AUTH_ENV
    ).stdout)

    plain = json.loads(cli.invoke(
        "parse", "-d", str(document), "--json", env=AUTH_ENV
    ).stdout)
    cached = json.loads(cli.invoke(
        "parse", "-d", str(document), "--include", "markdown", "--json", env=AUTH_ENV
    ).stdout)

    assert "markdown" not in plain
    assert cached["cached"] is True
    assert cached["markdown"] == fresh["markdown"]


def next_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.strip().startswith("next:"))


def test_summary_names_the_store_dir_and_next_commands(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    fresh = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    cached = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)  # from store

    # The cached-hit path serves the same summary (#34): both runs teach
    # where the artifacts live and what to run next.
    for result in (fresh, cached):
        assert result.exit_code == 0, result.stdout
        assert (
            f"saved:   {item_dir(cli, document)}/  (parse.json, parse.md, elements.json)"
            in result.stdout
        )
        line = next_line(result.stdout)
        assert "ade view " in line and "--open" in line
        assert "ade extract " in line and "--schema" in line
        # The hinted id prefix is runnable: it resolves today.
        ref = line.split("ade view ", 1)[1].split()[0]
        assert ref != item_dir(cli, document).name  # a prefix, not the full id
        assert item_dir(cli, document).name.startswith(ref)
        found = cli.invoke("find", "--job", ref, "--json")
        assert found.exit_code == 0, found.stdout
        assert json.loads(found.stdout)[0]["job_item_id"] == item_dir(cli, document).name


@pytest.mark.parametrize("command", ["extract", "view", "crop"])
def test_help_teaches_the_job_item_vocabulary(cli, command):
    result = cli.invoke(command, "--help")

    assert result.exit_code == 0
    # Collapse rich's help-panel wrapping before matching the phrase.
    assert "job item" in " ".join(result.stdout.lower().split())


def test_parse_without_credentials_fails_with_remediation(cli, document):
    result = cli.invoke("parse", "-d", str(document))

    assert result.exit_code == 1
    assert "auth login" in result.stdout
    assert cli.transport.requests == []  # nothing submitted, nothing billed


def test_parse_requires_exactly_one_source(cli, document):
    neither = cli.invoke("parse", env=AUTH_ENV)
    both = cli.invoke(
        "parse", "-d", str(document), "--document-url", "https://x.com/a.pdf",
        env=AUTH_ENV,
    )

    assert neither.exit_code == 2
    assert both.exit_code == 2
    assert cli.transport.requests == []


def test_wait_zero_submits_and_returns_without_polling(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})

    result = cli.invoke("parse", "-d", str(document), "--wait", "0", "--json", env=AUTH_ENV)

    assert result.exit_code == 3  # pending is a distinct outcome, not failure
    payload = json.loads(result.stdout)
    assert payload["status"] == "pending"
    assert payload["run_id"] == JOB_ID
    assert payload["job_item_id"] == item_dir(cli, document).name
    assert len(cli.transport.requests) == 1  # submit only, no poll
    assert json.loads((item_dir(cli, document) / "job.json").read_text())["job_id"] == JOB_ID


def test_invalid_tier_is_rejected_before_submission(cli, document):
    result = cli.invoke("parse", "-d", str(document), "--tier", "typo", env=AUTH_ENV)

    assert result.exit_code == 2
    assert cli.transport.requests == []  # nothing submitted, nothing billed


def test_budget_exhaustion_never_polls_past_the_deadline(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, job_payload("pending"))

    result = cli.invoke("parse", "-d", str(document), "--wait", "1", env=AUTH_ENV)

    assert result.exit_code == 3
    assert len(cli.transport.requests) == 2  # submit + one poll inside budget


def test_a_401_names_the_relogin_remediation_not_the_server_text(cli, document):
    # The platform's 401 bodies vary by which check rejected the key
    # ("Invalid API Key Format" vs "Invalid API Key, please check…", #117).
    # The human line is one canonical sentence with the remediation; the
    # server's own text rides only in the machine payload.
    bodies = [
        {"error": "Invalid API Key Format"},
        {"error": "Invalid API Key, please check that your API key is "
         "complete and entered correctly."},
    ]
    lines = []
    for body in bodies:
        cli.transport.respond(401, body)
        result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
        assert result.exit_code == 1
        lines.append(result.output)

    assert lines[0] == lines[1]
    assert "HTTP 401" in lines[0]
    assert "ade auth login" in lines[0]
    assert "Invalid API Key Format" not in lines[0]


def test_a_401_json_payload_carries_the_parsed_server_message(cli, document):
    # The auth layer's {"error": ...} envelope parses like the v2 ones —
    # the payload's message is the server's text, never raw JSON.
    cli.transport.respond(401, {"error": "Invalid API Key Format"})

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status_code"] == 401
    assert payload["message"] == "Invalid API Key Format"


def test_submit_http_failure_is_a_controlled_error(cli, document):
    cli.transport.respond(401, {"detail": "invalid api key"})

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)  # stable JSON error, no traceback
    assert payload["error"] == "http"
    assert payload["status_code"] == 401
    assert "invalid api key" in payload["message"]
    # A deterministic 4xx releases the claim: the corrected invocation
    # submits immediately instead of waiting out the lease grace.
    item = next((cli.home / "jobs").iterdir())
    assert not (item / "job.json").exists()
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    assert cli.invoke("parse", "-d", str(document), env=AUTH_ENV).exit_code == 0


def test_poll_http_failure_is_a_controlled_error(cli, document):
    # A non-retryable 4xx (404 has its own expiry path; 5xx is retried as
    # a pending tick, #19) stays a controlled error, never a traceback.
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(403, {"detail": "key revoked"})

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "http"
    assert payload["status_code"] == 403


def test_completed_without_inline_result_is_a_controlled_error(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    body = job_payload("completed", progress=1.0)
    body["output_url"] = "https://bucket.example.com/result.json"  # result stays null
    cli.transport.respond(200, body)

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)  # controlled output, no traceback
    assert payload["error"] == "missing_result"
    assert payload["run_id"] == JOB_ID


def test_unknown_job_status_is_a_controlled_error(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, job_payload("archived"))

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "unexpected_status"
    assert payload["status"] == "archived"


def test_cancelled_job_is_a_terminal_non_success(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, job_payload("cancelled"))

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "cancelled"
    assert not (item_dir(cli, document) / "parse.json").exists()


def test_poll_backs_off_through_the_injected_clock(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    for _ in range(4):
        cli.transport.respond(200, job_payload("pending"))
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 0
    assert cli.clock.sleeps == [1.0, 1.5, 2.25, 3.375]  # x1.5 backoff


def test_summary_and_payload_name_the_server_run_never_job(cli, document):
    """#153: the server-side id is a *run* everywhere user-facing — the
    summary line and the payload key — while "job" survives only inside
    "job item". (The wire and the on-disk store still spell job_id.)"""
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    human = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    assert human.exit_code == 0
    assert f"\n  run:     {JOB_ID}" in human.stdout
    assert "job:" not in human.stdout
    assert "job item" in human.stdout  # the local unit keeps its name

    payload = json.loads(
        cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV).stdout
    )
    assert payload["run_id"] == JOB_ID
    assert "job_id" not in payload
    # The store format is deliberately unrenamed.
    ticket = json.loads((item_dir(cli, document) / "job.json").read_text())
    assert ticket["job_id"] == JOB_ID


def test_pending_payload_names_the_run_never_job(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})

    result = cli.invoke(
        "parse", "-d", str(document), "--wait", "0", "--json", env=AUTH_ENV
    )

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "pending",
        "run_id": JOB_ID,
        "job_item_id": item_dir(cli, document).name,
    }
