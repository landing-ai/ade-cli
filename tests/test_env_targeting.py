"""Per-invocation environment targeting on the network verbs (ADR-0003).

No stored selection exists: ``--env`` → ``ADE_ENV`` → production decides
where a parse/extract goes and which credential signs it, per command.
The environment is part of job-item identity — one environment's result
never serves another's request — and an extract over a parse item
inherits the item's environment, because the server-side parse job id it
references exists nowhere else.
"""

from __future__ import annotations

import json

from ade_cli import store as jobstore

from extract_fixtures import completed_extract_job
from parse_fixtures import JOB_ID, completed_job

KEY = "sk-test-0123456789abcd"
DOC_BYTES = b"%PDF-1.4 fake invoice bytes"

STAGING = "https://api.ade.staging.landing.ai"
PRODUCTION = "https://api.ade.landing.ai"


def make_document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(DOC_BYTES)
    return path


def schema_file(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "object", "properties": {"total": {"type": "string"}}}))
    return path


def login(cli, environment, key):
    cli.transport.respond(200, {"accepted": 0})  # the verification probe (#117)
    result = cli.invoke("auth", "login", "--api-key", key, "--env", environment)
    assert result.exit_code == 0
    # These tests reason about the *verb's* traffic by request index; the
    # login probe is not part of that story.
    cli.transport.requests.clear()


def parse_in(cli, document, *args, job_id=JOB_ID, env=None):
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_job(job_id=job_id))
    result = cli.invoke("parse", "-d", str(document), "--json", *args, env=env)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_parse_env_targets_that_environment_with_its_own_credential(cli, tmp_path):
    staging_key = "sk-staging-1111aaaa"
    login(cli, "staging", staging_key)
    document = make_document(tmp_path)

    payload = parse_in(cli, document, "--env", "staging")

    submit = cli.transport.requests[0]
    assert str(submit.url).startswith(STAGING)
    assert submit.headers["Authorization"] == f"Bearer {staging_key}"
    assert payload["environment"] == "staging"
    meta = json.loads(
        (cli.home / "jobs" / payload["job_item_id"] / "meta.json").read_text()
    )
    assert meta["environment"] == "staging"


def test_ade_env_targets_parse_and_the_flag_beats_it(cli, tmp_path):
    login(cli, "staging", "sk-staging-1111aaaa")
    login(cli, "dev", "sk-dev-2222bbbb")
    document = make_document(tmp_path)

    ambient = parse_in(cli, document, env={"ADE_ENV": "staging"})
    flagged = parse_in(
        cli, document, "--env", "dev", env={"ADE_ENV": "staging"}, job_id="job-0002"
    )

    assert ambient["environment"] == "staging"
    assert flagged["environment"] == "dev"
    assert str(cli.transport.requests[0].url).startswith(STAGING)
    assert str(cli.transport.requests[2].url).startswith("https://api.ade.dev.landing.ai")


def test_environments_keep_separate_results_for_the_same_document(cli, tmp_path):
    # Same doc, same params, two environments ⇒ two sibling items: a
    # staging result must never satisfy a production request.
    login(cli, "staging", "sk-staging-1111aaaa")
    login(cli, "production", "sk-prod-2222bbbb")
    document = make_document(tmp_path)

    staging = parse_in(cli, document, "--env", "staging")
    production = parse_in(cli, document, job_id="job-0002")  # not cached: re-submits

    assert staging["job_item_id"] != production["job_item_id"]
    assert production["cached"] is False
    assert len(cli.transport.requests) == 4  # two submits, two polls


def test_parse_unknown_env_is_a_loud_usage_error_before_any_network(cli, tmp_path):
    document = make_document(tmp_path)

    result = cli.invoke(
        "parse", "-d", str(document), "--env", "qa", "--json",
        env={"ADE_API_KEY": KEY},
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "unknown_environment"
    assert cli.transport.requests == []


def test_parse_unauthenticated_remediation_names_the_env(cli, tmp_path):
    document = make_document(tmp_path)

    result = cli.invoke("parse", "-d", str(document), "--env", "staging")

    assert result.exit_code == 1
    assert "ade auth login --env staging" in result.output
    assert cli.transport.requests == []


def test_extract_of_a_parse_item_inherits_its_environment(cli, tmp_path):
    # The parse item pins the environment — even against an ambient
    # ADE_ENV pointing elsewhere — because the server-side parse job id it
    # references exists only there.
    staging_key = "sk-staging-1111aaaa"
    login(cli, "staging", staging_key)
    document = make_document(tmp_path)
    parse_item = parse_in(cli, document, "--env", "staging")["job_item_id"]

    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())
    result = cli.invoke(
        "extract", parse_item, "--schema", str(schema_file(tmp_path)), "--json",
        env={"ADE_ENV": "production", "ADE_API_KEY": None},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["environment"] == "staging"
    submit = cli.transport.requests[2]
    assert str(submit.url).startswith(STAGING)
    assert submit.headers["Authorization"] == f"Bearer {staging_key}"
    meta = json.loads(
        (cli.home / "jobs" / payload["job_item_id"] / "meta.json").read_text()
    )
    assert meta["environment"] == "staging"


def test_extract_with_a_conflicting_env_flag_is_refused(cli, tmp_path):
    login(cli, "staging", "sk-staging-1111aaaa")
    document = make_document(tmp_path)
    parse_item = parse_in(cli, document, "--env", "staging")["job_item_id"]
    before = len(cli.transport.requests)

    result = cli.invoke(
        "extract", parse_item, "--schema", str(schema_file(tmp_path)),
        "--env", "production", "--json",
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "environment_mismatch"
    assert payload["item_environment"] == "staging"
    assert len(cli.transport.requests) == before  # refused before any billing


def test_extract_d_never_reuses_another_environments_parse(cli, tmp_path):
    # extract -d on staging with only a *production* parse stored must run
    # a parse-first job on staging, not reuse across environments.
    login(cli, "staging", "sk-staging-1111aaaa")
    login(cli, "production", "sk-prod-2222bbbb")
    document = make_document(tmp_path)
    parse_in(cli, document)  # production parse of the same bytes

    cli.transport.respond(202, {"job_id": "job-0002"})  # staging parse-first
    cli.transport.respond(200, completed_job(job_id="job-0002"))
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(200, completed_extract_job())
    result = cli.invoke(
        "extract", "-d", str(document), "--schema", str(schema_file(tmp_path)),
        "--env", "staging", "--json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["environment"] == "staging"
    assert "parsed_first" in payload  # billed a fresh staging parse
    assert str(cli.transport.requests[2].url).startswith(STAGING)


def test_summary_names_a_non_default_environment(cli, tmp_path):
    login(cli, "staging", "sk-staging-1111aaaa")
    document = make_document(tmp_path)

    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    result = cli.invoke("parse", "-d", str(document), "--env", "staging")

    assert result.exit_code == 0
    assert "env:" in result.stdout and "staging" in result.stdout


def test_summary_stays_quiet_about_the_default_environment(cli, tmp_path):
    document = make_document(tmp_path)

    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    result = cli.invoke(
        "parse", "-d", str(document), env={"ADE_API_KEY": KEY}
    )

    assert result.exit_code == 0
    assert "env:" not in result.stdout


def test_unauthenticated_status_still_lists_authenticated_environments(cli):
    # Flagless status on a machine only logged into staging: the target
    # (production) is unauthenticated, but the staging credential must be
    # discoverable — that's how you learn what --env can reach.
    login(cli, "staging", "sk-staging-1111aaaa")

    result = cli.invoke("auth", "status")

    assert result.exit_code == 1
    assert "Not authenticated" in result.stdout
    assert "staging (api_key)" in result.stdout
    assert "target with --env" in result.stdout


def test_identity_pins_the_environment_component(cli, tmp_path):
    login(cli, "staging", "sk-staging-1111aaaa")
    document = make_document(tmp_path)

    payload = parse_in(cli, document, "--env", "staging")

    assert payload["job_item_id"] == jobstore.derive_id(
        "parse",
        "staging",
        jobstore.local_identity(document, DOC_BYTES),
        {"model": "dpt-3-pro-latest", "options": {}, "tier": "priority"},
    )
