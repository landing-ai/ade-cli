"""Usage ledger (#52): one event per invocation for every command in
the tree, names never values, opt-out honored, and telemetry failure
invisible to the command."""

from __future__ import annotations

import json
from importlib.metadata import version as installed_version

import pytest
from typer.main import get_command

from ade_cli.main import app
from ade_cli.telemetry import LEDGER_NAME


def events(cli) -> list[dict]:
    path = cli.home / LEDGER_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _leaf_paths(command, prefix=()):
    children = getattr(command, "commands", None)
    if children:
        for name, sub in sorted(children.items()):
            yield from _leaf_paths(sub, prefix + (name,))
    else:
        yield prefix


LEAVES = sorted(_leaf_paths(get_command(app)))


# --- one event per invocation, whole tree ---


def test_the_walked_tree_matches_the_expected_commands():
    # The parametrized walk below is only as good as the tree it walks;
    # pin the leaf set so a registration change is a loud diff here.
    assert LEAVES == sorted(
        [
            ("auth", "login"),
            ("auth", "logout"),
            ("auth", "status"),
            ("crop",),
            ("extract",),
            ("find",),
            ("help",),
            ("history", "clear"),
            ("history", "list"),
            ("login",),
            ("logout",),
            ("parse",),
            ("update",),
            ("version",),
            ("view",),
        ]
    )


@pytest.mark.parametrize("path", LEAVES, ids=lambda p: " ".join(p))
def test_every_command_in_the_tree_appends_one_event(cli, path):
    result = cli.invoke(*path, "--help")

    assert result.exit_code == 0
    (event,) = events(cli)
    assert event["command"] == " ".join(path)
    assert event["outcome"] == "success"


def test_a_bare_invocation_records_a_root_event(cli):
    cli.invoke()

    (event,) = events(cli)
    assert event["command"] == "(root)"


def test_an_unknown_command_records_unknown_not_the_typed_text(cli):
    result = cli.invoke("frobnicate")

    assert result.exit_code == 2
    (event,) = events(cli)
    assert event["command"] == "(unknown)"
    assert "frobnicate" not in (cli.home / LEDGER_NAME).read_text()


# --- event shape ---


def test_a_success_event_carries_the_documented_fields(cli):
    result = cli.invoke("version")

    assert result.exit_code == 0
    (event,) = events(cli)
    assert event["command"] == "version"
    assert event["flags"] == []
    assert event["outcome"] == "success"
    assert event["exit_code"] == 0
    assert isinstance(event["duration_ms"], int) and event["duration_ms"] >= 0
    assert event["version"] == installed_version("ade-cli")
    assert isinstance(event["ts"], float)
    # The harness shields ambient markers: no agent host, captured
    # streams are not ttys.
    assert event["host"] is None
    assert event["term"] == "non-tty"
    # No config in the temp home: the default environment applies.
    assert event["env"] == "production"


def test_events_carry_the_agent_host_and_terminal(cli):
    cli.invoke("version", env={"CLAUDECODE": "1", "TERM_PROGRAM": "iTerm.app"})

    (event,) = events(cli)
    assert event["host"] == "claude-code"
    assert event["term"] == "iterm"


def test_a_usage_error_records_as_usage_error(cli):
    result = cli.invoke("parse")  # missing required argument

    assert result.exit_code == 2
    (event,) = events(cli)
    assert event["command"] == "parse"
    assert event["outcome"] == "usage-error"
    assert event["exit_code"] == 2


def test_a_failing_command_records_as_failure(cli):
    # An unknown job token is a store-served failure: exit 1, no API.
    result = cli.invoke("view", "nonexistent-id")

    assert result.exit_code == 1
    (event,) = events(cli)
    assert event["command"] == "view"
    assert event["outcome"] == "failure"
    assert "nonexistent-id" not in (cli.home / LEDGER_NAME).read_text()


# --- names, never values ---


def test_events_record_flag_names_but_never_values_or_arguments(cli):
    doc = cli.home.parent / "confidential-invoice.pdf"
    doc.write_bytes(b"%PDF fake")
    cli.invoke("parse", str(doc), "--model", "dpt-3-secret", "--json")

    (event,) = events(cli)
    assert event["flags"] == ["--model", "--json"]
    text = (cli.home / LEDGER_NAME).read_text()
    assert "confidential-invoice" not in text
    assert "dpt-3-secret" not in text


def test_equals_form_flags_record_the_name_only(cli):
    cli.invoke("find", "needle-value", "--limit=7")

    (event,) = events(cli)
    assert "--limit" in event["flags"]
    text = (cli.home / LEDGER_NAME).read_text()
    assert "needle-value" not in text
    assert "7" not in json.dumps(event["flags"])


def test_a_negative_number_value_is_not_mistaken_for_a_flag(cli):
    cli.invoke("find", "x", "--limit", "-1")

    (event,) = events(cli)
    assert event["flags"] == ["--limit"]


def test_a_value_shaped_like_a_flag_is_not_recorded(cli):
    # Only *declared* option names may record: a value that merely looks
    # like a flag (or a typo'd flag) never rides into an event.
    doc = cli.home.parent / "doc.pdf"
    doc.write_bytes(b"%PDF fake")
    cli.invoke("parse", "-d", str(doc), "--model", "--sneaky-value")

    (event,) = events(cli)
    assert event["flags"] == ["-d", "--model"]
    assert "sneaky" not in (cli.home / LEDGER_NAME).read_text()


def test_a_typoed_flag_is_not_recorded(cli):
    cli.invoke("version", "--jsno")

    (event,) = events(cli)
    assert event["flags"] == []
    assert "jsno" not in (cli.home / LEDGER_NAME).read_text()


def test_the_stdin_sentinel_counts_as_positional_not_root(cli):
    cli.invoke("-")

    (event,) = events(cli)
    assert event["command"] == "(unknown)"


def test_tokens_after_the_separator_count_as_positional_not_root(cli):
    cli.invoke("--", "-x")

    (event,) = events(cli)
    assert event["command"] == "(unknown)"


# --- the API environment in effect (--env flag → ADE_ENV → production,
# per config.resolve_target; ADE_ENDPOINT overrides the URL alone) ---


def test_the_default_environment_is_recorded(cli):
    cli.invoke("version")

    (event,) = events(cli)
    assert event["env"] == "production"


def test_the_env_flag_is_recorded(cli):
    cli.invoke("find", "x", "--env", "staging")

    (event,) = events(cli)
    assert event["env"] == "staging"


def test_the_equals_form_env_flag_is_recorded(cli):
    cli.invoke("find", "x", "--env=eu")

    (event,) = events(cli)
    assert event["env"] == "eu"


def test_the_ambient_ade_env_is_recorded(cli):
    cli.invoke("version", env={"ADE_ENV": "staging"})

    (event,) = events(cli)
    assert event["env"] == "staging"


def test_switching_environments_between_commands_records_each(cli):
    cli.invoke("version", env={"ADE_ENV": "staging"})
    cli.invoke("version", env={"ADE_ENV": "eu"})

    assert [e["env"] for e in events(cli)] == ["staging", "eu"]


def test_an_endpoint_override_maps_back_to_its_environment_name(cli):
    cli.invoke("version", env={"ADE_ENDPOINT": "https://api.ade.dev.landing.ai"})

    (event,) = events(cli)
    assert event["env"] == "dev"


def test_a_custom_endpoint_records_the_bucket_never_the_url(cli):
    cli.invoke("version", env={"ADE_ENDPOINT": "https://ade.internal.example.com"})

    (event,) = events(cli)
    assert event["env"] == "custom"
    assert "internal.example.com" not in (cli.home / LEDGER_NAME).read_text()


def test_an_unknown_environment_name_records_unknown_not_the_text(cli):
    cli.invoke("version", env={"ADE_ENV": "my-secret-env"})

    (event,) = events(cli)
    assert event["env"] == "unknown"
    assert "my-secret-env" not in (cli.home / LEDGER_NAME).read_text()


# --- opt-out ---


@pytest.mark.parametrize(
    "env", [{"ADE_TELEMETRY": "0"}, {"DO_NOT_TRACK": "1"}], ids=["ade-telemetry", "dnt"]
)
def test_opt_out_produces_no_ledger_writes(cli, env):
    result = cli.invoke("version", env=env)

    assert result.exit_code == 0
    assert not (cli.home / LEDGER_NAME).exists()


# --- telemetry failure is invisible ---


def test_an_unwritable_ledger_changes_nothing_about_the_command(cli):
    baseline = cli.invoke("version", env={"ADE_TELEMETRY": "0"})
    (cli.home / LEDGER_NAME).mkdir(parents=True)  # append will fail

    result = cli.invoke("version")

    assert result.exit_code == baseline.exit_code == 0
    assert result.stdout == baseline.stdout
    assert result.stderr == baseline.stderr


def test_a_corrupt_ledger_still_appends_and_never_surfaces(cli):
    cli.home.mkdir(parents=True)
    (cli.home / LEDGER_NAME).write_text("{not json\n")

    result = cli.invoke("version")

    assert result.exit_code == 0
    lines = (cli.home / LEDGER_NAME).read_text().splitlines()
    assert lines[0] == "{not json"
    assert json.loads(lines[1])["command"] == "version"
