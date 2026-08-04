"""The parse guarantee: idempotent, resumable, interrupt-safe (issue #4).

Every test drives the CLI seam; billing-visible behavior (exactly one
submit per guarantee) is asserted on the fake transport's request log.
"""

import json

import pytest

from parse_fixtures import JOB_ID, completed_job, job_payload, parse_response

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}
DOC_BYTES = b"%PDF-1.4 fake invoice bytes"


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(DOC_BYTES)
    return path


def posts(cli):
    return [r for r in cli.transport.requests if r.method == "POST"]


def complete_one_parse(cli, document, **kwargs):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job(**kwargs))
    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    assert result.exit_code == 0
    return result


def test_rerunning_a_completed_parse_makes_zero_http_calls(cli, document):
    complete_one_parse(cli, document)
    seen = len(cli.transport.requests)

    again = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert again.exit_code == 0
    assert len(cli.transport.requests) == seen  # served from disk
    payload = json.loads(again.stdout)
    assert payload["status"] == "parsed"
    assert payload["run_id"] == JOB_ID  # summary still traceable to the bill


def test_exact_rerun_prints_the_already_parsed_notice(cli, document):
    # Dedup-with-notice: the free cache hit says so explicitly, names the
    # job item and when it completed, and teaches the --force override.
    complete_one_parse(cli, document)

    again = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert again.exit_code == 0
    (item_dir,) = (cli.home / "jobs").iterdir()
    notice = f"already parsed — job item {item_dir.name} (completed "
    assert notice in again.stdout
    assert "pass --force to re-parse" in again.stdout


def test_force_reparses_a_completed_parse(cli, document):
    complete_one_parse(cli, document)
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))

    result = cli.invoke("parse", "-d", str(document), "--force", "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"
    assert len(posts(cli)) == 2


def test_rerun_resumes_a_pending_job_without_a_second_submit(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    first = cli.invoke("parse", "-d", str(document), "--wait", "0", env=AUTH_ENV)
    assert first.exit_code == 3  # ticket saved, job running server-side

    cli.transport.respond(200, completed_job())
    second = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert second.exit_code == 0
    assert json.loads(second.stdout)["run_id"] == JOB_ID
    assert len(posts(cli)) == 1  # exactly one submit across both runs


def test_interrupt_mid_poll_leaves_a_resumable_ticket(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, job_payload("pending"))
    cli.clock.interrupt_sleep_at = 0  # Ctrl-C at the first backoff sleep

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 3
    assert "server-side" in result.stdout
    item_dirs = list((cli.home / "jobs").iterdir())
    ticket = json.loads((item_dirs[0] / "job.json").read_text())
    assert ticket == {**ticket, "job_id": JOB_ID, "state": "pending"}

    cli.clock.interrupt_sleep_at = None
    cli.transport.respond(200, completed_job())
    resumed = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    assert resumed.exit_code == 0
    assert len(posts(cli)) == 1  # interrupt never causes a second submit


def test_pending_output_advises_rerunning_now_that_resume_exists(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})

    result = cli.invoke("parse", "-d", str(document), "--wait", "0", env=AUTH_ENV)

    assert result.exit_code == 3
    assert "re-run" in result.stdout.lower()


def test_a_submitless_ticket_is_reclaimed(cli, document):
    # Crash window: claim written, process died before submit ⇒ no job_id.
    complete_one_parse(cli, document)
    item_dirs = list((cli.home / "jobs").iterdir())
    (item_dirs[0] / "meta.json").unlink()  # not parsed either
    (item_dirs[0] / "job.json").write_text(
        json.dumps({"job_id": None, "tier": "priority", "submitted_at": 0, "state": "pending"})
    )
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"


def test_pending_ticket_for_different_params_is_not_resumed(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.invoke("parse", "-d", str(document), "--wait", "0", env=AUTH_ENV)

    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    result = cli.invoke(
        "parse", "-d", str(document), "--model", "dpt-3-pro-20260515", "--json",
        env=AUTH_ENV,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"
    assert len(posts(cli)) == 2  # different guarantee, deliberate new submit


def test_failed_parse_is_reported_once_then_resubmitted_fresh(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, job_payload("failed", failure_reason="ocr blew up"))
    first = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    assert first.exit_code == 1
    assert "ocr blew up" in first.stdout

    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    second = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert second.exit_code == 0
    assert json.loads(second.stdout)["run_id"] == "job-0002"
    assert len(posts(cli)) == 2  # one fresh resubmit, not a resume


def test_expired_pending_job_is_treated_as_absent_and_resubmitted(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.invoke("parse", "-d", str(document), "--wait", "0", env=AUTH_ENV)

    cli.transport.respond(404, None)  # server retention passed: poll 404s
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"
    assert len(posts(cli)) == 2


def test_poll_5xx_is_retried_like_a_pending_tick(cli, document):
    # Observed live (issue #19): a transient 500 (empty body) from the
    # gateway killed a run whose job was already completed — the very next
    # poll of the same job returned 200. One invocation must ride it out.
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(500, None)
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == JOB_ID
    assert len(posts(cli)) == 1  # the submit POST is never blindly retried
    assert 1.0 in cli.clock.sleeps  # backed off like a pending tick


def test_poll_only_5xx_exits_pending_at_the_wait_budget(cli, document):
    # A persistently down server is a pending outcome, not a hang and not
    # a hard failure: the budget bounds the retrying, the ticket survives.
    cli.transport.respond(202, {"job_id": JOB_ID})
    for _ in range(8):  # more than the budget can consume
        cli.transport.respond(500, {"detail": "upstream connect error"})

    result = cli.invoke(
        "parse", "-d", str(document), "--wait", "5", "--json", env=AUTH_ENV
    )

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["status"] == "pending"
    assert payload["run_id"] == JOB_ID
    assert sum(cli.clock.sleeps) <= 5  # never sleeps past the promised budget
    # Ticket intact: the next invocation re-joins the same job for free.
    item_dir = next((cli.home / "jobs").iterdir())
    ticket = json.loads((item_dir / "job.json").read_text())
    assert ticket["job_id"] == JOB_ID
    assert ticket["state"] == "pending"
    assert len(posts(cli)) == 1


def test_poll_503_honors_retry_after(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(503, {"detail": "maintenance"}, headers={"Retry-After": "7"})
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert 7.0 in cli.clock.sleeps  # server-provided delay honored, not 1.0


def test_poll_5xx_retry_after_zero_does_not_spin(cli, document):
    # Retry-After is a minimum, not an override downward: a 5xx storm
    # with Retry-After: 0 must not turn the poll into a zero-sleep spin.
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(503, {"detail": "flapping"}, headers={"Retry-After": "0"})
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert 0.0 not in cli.clock.sleeps  # the backoff stays the floor
    assert 1.0 in cli.clock.sleeps


def test_poll_503_retry_after_past_the_budget_exits_pending(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(503, {"detail": "maintenance"}, headers={"Retry-After": "60"})

    result = cli.invoke(
        "parse", "-d", str(document), "--wait", "5", "--json", env=AUTH_ENV
    )

    assert result.exit_code == 3
    assert json.loads(result.stdout)["status"] == "pending"
    assert 60.0 not in cli.clock.sleeps  # never sleeps past the promised budget


def test_submit_429_retries_with_retry_after_inside_the_budget(cli, document):
    cli.transport.respond(429, {"detail": "hourly page bucket"}, headers={"Retry-After": "7"})
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert 7.0 in cli.clock.sleeps
    assert len(posts(cli)) == 2


def test_submit_429_exhausting_the_budget_is_a_distinct_rate_limited_state(cli, document):
    cli.transport.respond(429, {"detail": "hourly page bucket"}, headers={"Retry-After": "10"})

    result = cli.invoke("parse", "-d", str(document), "--wait", "5", "--json", env=AUTH_ENV)

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "rate_limited"
    assert len(posts(cli)) == 1
    # nothing was submitted, so no claim ticket survives
    item_dirs = list((cli.home / "jobs").iterdir())
    assert not (item_dirs[0] / "job.json").exists()


def test_failed_reparse_does_not_cache_hit_the_old_parse(cli, document):
    complete_one_parse(cli, document)
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, job_payload("failed", job_id="job-0002", failure_reason="boom"))
    forced = cli.invoke("parse", "-d", str(document), "--force", env=AUTH_ENV)
    assert forced.exit_code == 1

    # the failure was reported once; the next plain run must resubmit fresh,
    # not silently serve the pre-force parse
    cli.transport.respond(202, {"job_id": "job-0003"})
    cli.transport.respond(200, completed_job(job_id="job-0003"))
    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0003"
    assert len(posts(cli)) == 3


def test_interrupt_during_submit_retry_sleep_is_a_controlled_pending_exit(cli, document):
    cli.transport.respond(429, {"detail": "bucket"}, headers={"Retry-After": "7"})
    cli.clock.interrupt_sleep_at = 0  # Ctrl-C while sleeping on Retry-After

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 3  # controlled, not a traceback
    assert result.exception is None or isinstance(result.exception, SystemExit)
    # Known pre-submit: the claim is released so the next run reclaims
    # immediately instead of waiting out the lease grace.
    item_dir = next((cli.home / "jobs").iterdir())
    assert not (item_dir / "job.json").exists()

    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    cli.clock.interrupt_sleep_at = None
    retry = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    assert retry.exit_code == 0


def test_zero_budget_with_retry_after_zero_exits_rate_limited(cli, document):
    cli.transport.respond(429, {"detail": "bucket"}, headers={"Retry-After": "0"})

    result = cli.invoke("parse", "-d", str(document), "--wait", "0", "--json", env=AUTH_ENV)

    assert result.exit_code == 4  # exhausted budget can never spin on RA:0
    assert json.loads(result.stdout)["status"] == "rate_limited"
    assert len(posts(cli)) == 1


def test_mixed_generation_artifacts_are_not_served_from_cache(cli, document):
    complete_one_parse(cli, document)
    item_dir = next((cli.home / "jobs").iterdir())
    # Simulate a crash mid-persist: parse.json belongs to a different
    # generation than meta.json (their job_ids disagree).
    mixed = json.loads((item_dir / "parse.json").read_text())
    mixed["metadata"]["job_id"] = "job-other-generation"
    (item_dir / "parse.json").write_text(json.dumps(mixed))

    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"  # re-parsed
    assert len(posts(cli)) == 2


def test_retry_after_zero_means_retry_immediately(cli, document):
    cli.transport.respond(429, {"detail": "bucket"}, headers={"Retry-After": "0"})
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 0
    assert 0.0 in cli.clock.sleeps  # server-provided delay honored, not 1.0


def test_stale_404_poller_joins_the_fresh_job_instead_of_stealing_it(cli, document):
    import httpx

    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.invoke("parse", "-d", str(document), "--wait", "0", env=AUTH_ENV)
    item_dir = next((cli.home / "jobs").iterdir())

    def expired_and_already_reclaimed_by_another_poller(request):
        # Between our 404 and our recovery, a concurrent poller has already
        # replaced the expired ticket with its own fresh claim + job.
        fresh = json.loads((item_dir / "job.json").read_text())
        fresh["job_id"] = "job-0002"
        (item_dir / "job.json").write_text(json.dumps(fresh))
        return httpx.Response(404, json=None)

    cli.transport.respond_with(expired_and_already_reclaimed_by_another_poller)
    cli.transport.respond(200, completed_job(job_id="job-0002"))

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"  # joined, not stolen
    assert len(posts(cli)) == 1  # the other poller's claim was never clobbered


def test_displaced_poller_does_not_publish_artifacts_over_the_newer_guarantee(cli, document):
    import httpx

    cli.transport.respond(202, {"job_id": JOB_ID})
    displaced_by = {
        "v": 1,
        "job_id": "job-newer",
        "tier": "priority",
        "params": {"model": "dpt-3-pro-20260515", "pages": None, "tier": "priority"},
        "submitted_at": 1_750_000_000.0,
        "state": "pending",
    }

    def completed_but_displaced(request):
        # A different-params guarantee replaced our claim while we polled.
        item_dir = next((cli.home / "jobs").iterdir())
        (item_dir / "job.json").write_text(json.dumps(displaced_by))
        return httpx.Response(200, json=completed_job())

    cli.transport.respond_with(completed_but_displaced)

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0  # our job did complete
    payload = json.loads(result.stdout)
    assert payload["stored"] is False  # ...but the store belongs to the newer run
    item_dir = next((cli.home / "jobs").iterdir())
    assert not (item_dir / "parse.json").exists()
    assert json.loads((item_dir / "job.json").read_text()) == displaced_by


def test_completed_with_output_url_reports_the_observed_url(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    delivered = job_payload("completed", progress=1.0)
    delivered["output_url"] = "https://results.example/job-0001.json"
    cli.transport.respond(200, delivered)

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "missing_result"
    assert payload["output_url"] == "https://results.example/job-0001.json"
    assert "output_url" in payload["payload_keys"]


def test_completed_under_a_data_key_names_the_contract_and_endpoint(cli, document):
    # The 2026-07-15 incident shape: a completed poll answered with the
    # payload under a top-level `data` key and no `result` at all — here
    # from a raw ADE_ENDPOINT target, whose URL the diagnosis must name.
    endpoint = "https://custom.internal.example.com"
    cli.transport.respond(202, {"job_id": JOB_ID})
    foreign = {**job_payload("completed", progress=1.0), "data": parse_response()}
    del foreign["result"], foreign["output_url"]
    cli.transport.respond(200, foreign)

    result = cli.invoke(
        "parse", "-d", str(document), env={**AUTH_ENV, "ADE_ENDPOINT": endpoint}
    )

    assert result.exit_code == 1
    assert endpoint in result.stdout  # names the endpoint actually configured
    assert "'data'" in result.stdout
    # The old message asserted URL delivery without evidence; it must not
    # misdirect debugging when the cause is a contract mismatch.
    assert "output_save_url" not in result.stdout
    # And it must not advise an upgrade: an up-to-date CLI is exactly what
    # fails here — the endpoint runs the pre-cutover API release (#32).
    assert "upgrade ade" not in result.stdout
    assert "pre-cutover" in result.stdout


def test_pre_cutover_diagnosis_names_the_configured_environment(cli, document):
    # When a named environment is configured, the diagnosis speaks in
    # environment terms — the actionable knob is `auth login --env`.
    cli.home.mkdir(parents=True)
    (cli.home / "config.json").write_text(json.dumps({"environment": "production"}))
    cli.transport.respond(202, {"job_id": JOB_ID})
    foreign = {**job_payload("completed", progress=1.0), "data": parse_response()}
    del foreign["result"], foreign["output_url"]
    cli.transport.respond(200, foreign)

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 1
    assert "the production environment (https://api.ade.landing.ai)" in result.stdout
    assert "--env" in result.stdout  # points at the environment switch


def test_completed_with_neither_result_nor_data_lists_the_observed_keys(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, job_payload("completed", progress=1.0))

    result = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)

    assert result.exit_code == 1
    assert "top-level keys" in result.stdout
    assert "progress" in result.stdout  # the shape observed, not a guessed cause


def test_unreadable_completion_marks_the_ticket_and_rerun_never_resubmits(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, {**job_payload("completed"), "data": {}})
    first = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    assert first.exit_code == 1

    item_dir = next((cli.home / "jobs").iterdir())
    ticket = json.loads((item_dir / "job.json").read_text())
    assert ticket["state"] == "unreadable"  # not pending: view must not advise a finishing re-run

    # The re-run joins the same completed job and re-polls it — never a
    # fresh submit, which would re-bill and read the same way.
    cli.transport.respond(200, {**job_payload("completed"), "data": {}})
    second = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert second.exit_code == 1
    payload = json.loads(second.stdout)
    assert payload["error"] == "missing_result"
    assert payload["output_url"] is None
    assert "data" in payload["payload_keys"]
    assert len(posts(cli)) == 1  # exactly one submit across both runs


def test_unreadable_ticket_recovers_by_repolling_the_same_job(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, {**job_payload("completed"), "data": {}})
    assert cli.invoke("parse", "-d", str(document), env=AUTH_ENV).exit_code == 1

    # The contract heals server-side: the same job's result publishes
    # without a second bill.
    cli.transport.respond(200, completed_job())
    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == JOB_ID
    assert len(posts(cli)) == 1

    # ...and the published parse serves from disk: unreadable never leaves
    # the resubmit-on-failure tripwire armed the way failed does.
    seen = len(cli.transport.requests)
    again = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    assert again.exit_code == 0
    assert len(cli.transport.requests) == seen


def pre_cutover_job():
    # The #31 hybrid: the new poll envelope, but the stored old-shape body
    # verbatim — split structure/grounding sibling trees, per-element span
    # with no embedded grounding — while metadata alone is re-projected
    # fresh, so the response *looks* current.
    old = parse_response()
    page = old["structure"]["children"][0]
    del page["grounding"]
    page.update({"page": 0, "dpi": 72, "width": 100, "height": 100, "span": [0, 1]})
    page["children"] = [{"id": "text-0", "type": "text", "span": [0, 1]}]
    old["grounding"] = {"type": "document", "children": []}
    return completed_job(old)


def test_unsupported_result_schema_fails_explicitly_before_any_write(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, pre_cutover_job())

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "unsupported_result_schema"
    assert "grounding" in payload["reason"]  # the failing access, as evidence
    item_dir = next((cli.home / "jobs").iterdir())
    # Rejected whole, before any artifact write: no torn parse.json/meta.json.
    assert not (item_dir / "parse.json").exists()
    assert not (item_dir / "meta.json").exists()
    ticket = json.loads((item_dir / "job.json").read_text())
    assert ticket["state"] == "unreadable"
    assert "grounding" in ticket["reason"]
    # The diagnosis is durable: history list surfaces it from the ticket,
    # in the JSON record and the human output alike.
    listed = json.loads(cli.invoke("history", "list", "--json", env=AUTH_ENV).stdout)
    assert listed[0]["state"] == "unreadable"
    assert "grounding" in listed[0]["reason"]
    human = cli.invoke("history", "list", env=AUTH_ENV).stdout
    assert "reason:" in human and "grounding" in human


def test_unsupported_schema_advises_upgrade_and_rerun_repolls_free(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, pre_cutover_job())
    first = cli.invoke("parse", "-d", str(document), env=AUTH_ENV)
    assert first.exit_code == 1
    # The CLI usually lags the API, so the default advice is an upgrade —
    # and the wrong-guess cost is a re-poll, never a bill.
    assert "upgrade ade (re-run the installer, or `uv tool upgrade ade-cli`)" in first.stdout
    assert "re-billing" in first.stdout

    # A later poll serves a readable shape (e.g. after a CLI upgrade or a
    # server fix): the same job publishes without a second submit.
    cli.transport.respond(200, completed_job())
    second = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert second.exit_code == 0
    assert json.loads(second.stdout)["run_id"] == JOB_ID
    assert len(posts(cli)) == 1  # billed once across all runs


def test_wrong_typed_result_fields_fail_explicitly_not_a_traceback(cli, document):
    # Type drift, not just missing keys: nodes where dicts are expected
    # (AttributeError on .get), markdown that isn't a string. Both must
    # land in the same explicit rejection, before any write.
    drifted = parse_response()
    drifted["structure"]["children"] = ["page-0"]  # ids instead of nodes
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job(drifted))

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "unsupported_result_schema"
    item_dir = next((cli.home / "jobs").iterdir())
    assert not (item_dir / "parse.json").exists()


def test_non_string_markdown_fails_explicitly_before_any_write(cli, document):
    drifted = parse_response()
    drifted["markdown"] = [drifted["markdown"]]  # e.g. per-page chunks
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job(drifted))

    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "unsupported_result_schema"
    item_dir = next((cli.home / "jobs").iterdir())
    assert not (item_dir / "parse.json").exists()


def test_force_abandons_an_unsupported_schema_job_and_resubmits(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, pre_cutover_job())
    assert cli.invoke("parse", "-d", str(document), env=AUTH_ENV).exit_code == 1

    # The stored body never heals server-side; --force is the consented
    # escape: abandon the unreadable job, submit (and bill) a fresh one.
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    result = cli.invoke("parse", "-d", str(document), "--force", "--json", env=AUTH_ENV)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"
    assert len(posts(cli)) == 2  # exactly one deliberate resubmit


def test_unreadable_ticket_with_different_params_resubmits_deliberately(cli, document):
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, {**job_payload("completed"), "data": {}})
    assert cli.invoke("parse", "-d", str(document), env=AUTH_ENV).exit_code == 1

    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    result = cli.invoke(
        "parse", "-d", str(document), "--model", "dpt-3-pro-20260515", "--json",
        env=AUTH_ENV,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == "job-0002"
    assert len(posts(cli)) == 2  # different guarantee, deliberate new submit


def test_changed_params_parse_is_a_sibling_variant(cli, document):
    # Params live inside identity: "last parse wins" is retired. A changed
    # model mints a sibling job item; the original run stays intact, still
    # true of the invocation it was computed from.
    complete_one_parse(cli, document)
    new_markdown = "# Reparsed\n\n<!-- doc_id=srv-doc-77aa00 -->\n"
    cli.transport.respond(202, {"job_id": "job-0002"})
    cli.transport.respond(
        200, completed_job(parse_response(markdown=new_markdown, job_id="job-0002"))
    )

    result = cli.invoke(
        "parse", "-d", str(document), "--model", "dpt-3-pro-20260515", env=AUTH_ENV
    )

    assert result.exit_code == 0
    assert len(posts(cli)) == 2
    items = sorted((cli.home / "jobs").iterdir())
    assert len(items) == 2  # variants coexist, side by side
    markdowns = {(d / "parse.md").read_text() for d in items}
    assert new_markdown in markdowns
    assert len(markdowns) == 2  # the original parse was never replaced
    models = {
        json.loads((d / "meta.json").read_text())["params"]["model"] for d in items
    }
    assert models == {"dpt-3-pro-latest", "dpt-3-pro-20260515"}
