"""``help`` and SKILL.md — the agent contract, drift-guarded against the
command tree.

The command/flag inventory in ``help`` is generated from the same click
tree that executes, so those tests are regression locks; the curated
layers (bands, exit states, store layout) and SKILL.md are hand-written
and genuinely can drift — the tests here are what pins them to the
shipped surface.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.core import TyperGroup
from typer.main import get_command

from ade_cli.extract import EXTRACT_ARTIFACTS
from ade_cli.main import app
from ade_cli.output import (
    EXIT_FAILED,
    EXIT_PENDING,
    EXIT_RATE_LIMITED,
    EXIT_USAGE,
)
from ade_cli.parse import PARSE_ARTIFACTS

SKILL = (Path(__file__).parents[1] / "SKILL.md").read_text(encoding="utf-8")

# The doc-id era's vocabulary must never resurface in the agent contract
# (issue #63): the `docs` command, doc ids, REF resolution, and the
# last-parse-wins rule are all retired.
RETIRED = [
    re.compile(r"\bdocs\b"),
    re.compile(r"\bdoc[ _-]ids?\b", re.IGNORECASE),
    re.compile(r"\bREF\b"),
    re.compile(r"last[ -]parse[ -]wins", re.IGNORECASE),
]


def _tree_commands() -> set[str]:
    """Every runnable, non-hidden command in the executable tree."""
    group = get_command(app)

    def walk(node, prefix=()):
        for name, command in node.commands.items():
            if command.hidden:
                continue
            if isinstance(command, TyperGroup):
                yield from walk(command, (*prefix, name))
            else:
                yield " ".join((*prefix, name))

    return set(walk(group))


@pytest.fixture
def reference(cli) -> dict:
    result = cli.invoke("help", "--json")
    assert result.exit_code == 0
    return json.loads(result.stdout)


def test_help_covers_the_whole_command_tree(reference):
    """One call teaches the entire shipped surface — every command in the
    tree appears in help, and help invents none."""
    assert {c["name"] for c in reference["commands"]} == _tree_commands()


def test_every_command_documents_flags_and_supports_json(reference):
    """The --json convention is stated once because it is universal; every
    non-flag option carries help text an agent can act on."""
    for command in reference["commands"]:
        assert command["supports_json"], f"{command['name']} lacks --json"
        assert command["summary"].strip(), f"{command['name']} lacks a summary"
        for flag in command["flags"]:
            assert flag["help"], f"{command['name']} {flag['flags']} lacks help"


def test_every_command_publishes_its_result_shape(reference):
    """F7: the --json shape is a published contract, not something a
    scripter discovers by running the command. A newly shipped command
    must bring its own result block."""
    for command in reference["commands"]:
        result = command["result"]
        assert result, f"{command['name']} publishes no result shape"
        assert result["shape"] in ("object", "array")
        assert result["keys"], f"{command['name']} documents no result keys"
        for entry in result["keys"]:
            assert entry["key"] and entry["what"], command["name"]


def test_result_shapes_name_the_keys_the_verbs_actually_emit(reference):
    """Spot-check the contract against the implementation for the two
    payloads agents key off — the pair most costly to get wrong."""
    by_name = {command["name"]: command for command in reference["commands"]}
    extract_keys = {e["key"] for e in by_name["extract"]["result"]["keys"]}
    # The result itself must be documented as living on stdout (F9).
    assert "extraction" in extract_keys
    assert {"job_item_id", "evidence", "credits"} <= extract_keys
    find_keys = {e["key"] for e in by_name["find"]["result"]["keys"]}
    assert find_keys == {"job_item_id", "element_id", "type", "page", "box", "text"}
    assert by_name["find"]["result"]["shape"] == "array"


def test_topics_are_reachable_and_never_shadow_a_command(reference, cli):
    """F6: the conceptual pages a per-command --help cannot hold. Each is
    reachable as `ade help TOPIC`, and no topic name shadows a command."""
    names = [topic["name"] for topic in reference["topics"]]
    assert "workflow" in names
    # Every scope a command answers to — leaf paths and the group prefixes
    # `help auth` resolves through. A topic taking one of those would hide
    # part of the surface behind a page.
    command_scopes = set()
    for name in _tree_commands():
        words = name.split()
        command_scopes.update(" ".join(words[:n]) for n in range(1, len(words) + 1))
    assert not set(names) & command_scopes
    for topic in reference["topics"]:
        assert topic["title"] and topic["body"]
        result = cli.invoke("help", topic["name"], "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["name"] == topic["name"]
    # ...and discoverable from the bare reference, not only by knowing.
    listing = cli.invoke("help").stdout
    for name in names:
        assert name in listing


def test_workflow_topic_teaches_the_whole_pipeline(reference):
    """The cold-read overview: every verb of the loop, in one page."""
    (workflow,) = [t for t in reference["topics"] if t["name"] == "workflow"]
    body = "\n".join(workflow["body"])
    for verb in ("parse", "find", "crop", "extract", "view"):
        assert verb in body, f"the workflow topic omits {verb}"


def test_unknown_scope_names_the_topics_too(cli):
    payload = json.loads(cli.invoke("help", "nosuch", "--json").stdout)
    assert "workflow" in payload["topics"]


def test_every_command_is_banded(reference):
    """A newly shipped command must be placed in the curated band grouping
    — the unbanded bucket renders (coverage never narrows) but must stay
    empty."""
    assert [c["name"] for c in reference["commands"] if c["band"] == "unbanded"] == []


def _shipped_exit_codes() -> set[int]:
    """Every EXIT_* constant output.py ships — introspected, so adding a
    new exit state fails these tests until help and SKILL.md document it."""
    import ade_cli.output as output

    return {0} | {
        value for name, value in vars(output).items() if name.startswith("EXIT_")
    }


def test_exit_states_document_the_implementation(reference):
    codes = {state["code"]: state["name"] for state in reference["exit_states"]}
    assert set(codes) == _shipped_exit_codes()
    assert codes == {
        0: "ok",
        EXIT_FAILED: "failed",
        EXIT_USAGE: "usage",
        EXIT_PENDING: "pending",
        EXIT_RATE_LIMITED: "rate_limited",
    }


def test_store_layout_names_the_shipped_artifacts(reference):
    layout = json.dumps(reference["store"]["layout"])
    for artifact in [
        *PARSE_ARTIFACTS,
        *EXTRACT_ARTIFACTS,
        "meta.json",
        "job.json",
        "parse/ref.json",
        "markdown.md",
        "view.html",
        "history.js",
        "jobs/<job-item-id>/",
    ]:
        assert artifact in layout, f"store layout omits {artifact}"


def test_committed_reference_matches_the_shipped_surface(reference):
    """docs/reference/help.json is the committed snapshot of the agent
    surface: any change to a command, flag, exit state, or the store
    layout must show up as a reviewable diff in the PR that made it (and
    prompt a SKILL.md narrative check). When this fails, regenerate:

        uv run python scripts/update_help_reference.py
    """
    committed = json.loads(
        (Path(__file__).parents[1] / "docs" / "reference" / "help.json").read_text(
            encoding="utf-8"
        )
    )
    live = {key: value for key, value in reference.items() if key != "version"}
    assert live == committed, (
        "the shipped surface changed — regenerate the snapshot with "
        "`uv run python scripts/update_help_reference.py`, review the diff, "
        "and check SKILL.md still tells the right story"
    )


def test_help_scopes_to_one_command(cli):
    result = cli.invoke("help", "extract", "--json")
    assert result.exit_code == 0
    assert [c["name"] for c in json.loads(result.stdout)["commands"]] == ["extract"]

    grouped = cli.invoke("help", "auth", "--json")
    assert [c["name"] for c in json.loads(grouped.stdout)["commands"]] == [
        "auth login",
        "auth status",
        "auth logout",
    ]


def test_help_unknown_command_is_a_usage_error(cli):
    result = cli.invoke("help", "nosuch", "--json")
    assert result.exit_code == EXIT_USAGE
    payload = json.loads(result.stdout)
    assert payload["error"] == "unknown_command"
    assert "parse" in payload["candidates"]


def test_help_speaks_no_retired_vocabulary(cli, reference):
    """Neither output mode may carry doc-id-era vocabulary."""
    human = cli.invoke("help").stdout
    for text in (human, json.dumps(reference)):
        for pattern in RETIRED:
            assert not pattern.search(text), f"retired vocabulary: {pattern.pattern}"


def test_network_verbs_never_mention_dpi(reference):
    """The parse API's dpi option is retired (422 since 2026-07-16); only
    the local render flag on view/crop may speak of dpi."""
    for command in reference["commands"]:
        if command["name"] in ("parse", "extract"):
            assert "dpi" not in json.dumps(command).lower(), (
                f"{command['name']} mentions dpi — the parse-time option is retired"
            )


# --- SKILL.md: the deployed agent contract ---------------------------------


def test_skill_speaks_no_retired_vocabulary():
    for pattern in RETIRED:
        assert not pattern.search(SKILL), f"retired vocabulary: {pattern.pattern}"
    # SKILL.md never needs the render flag either; any dpi mention risks
    # reading as the retired parse option.
    assert "dpi" not in SKILL.lower()


def test_skill_exit_code_table_matches_implementation():
    rows = dict(re.findall(r"^\|\s*(\d+)\s*\|\s*(\w+)\s*\|", SKILL, re.MULTILINE))
    assert {int(code) for code in rows} == _shipped_exit_codes()
    assert rows == {
        "0": "ok",
        str(EXIT_FAILED): "failed",
        str(EXIT_USAGE): "usage",
        str(EXIT_PENDING): "pending",
        str(EXIT_RATE_LIMITED): "rate_limited",
    }


def test_skill_commands_exist_in_the_tree():
    """Every `ade <command>` SKILL.md teaches must be shipped."""
    tree = _tree_commands()
    mentioned = set()
    # Only code is a command mention — fenced blocks and inline spans;
    # prose like "ade drives the ADE v2 APIs" is not a command.
    code = "\n".join(
        re.findall(r"```(?:\w*\n)?(.*?)```", SKILL, re.DOTALL)
        + re.findall(r"`([^`\n]+)`", SKILL)
    )
    for tokens in re.findall(r"(?<![\w./~-])ade ((?:[a-z]+ ?)+)", code):
        words = tokens.strip().split()
        # The longest prefix of the mention that is a real command path
        # ("history list --json" → "history list", "parse -d" → "parse").
        for take in range(len(words), 0, -1):
            candidate = " ".join(words[:take])
            if candidate in tree:
                mentioned.add(candidate)
                break
        else:
            raise AssertionError(f"SKILL.md names an unshipped command: {tokens!r}")
    # The loop SKILL.md teaches touches the store surface end to end.
    assert {"parse", "extract", "find", "view", "crop", "help"} <= mentioned


def test_skill_teaches_the_reuse_posture():
    """Acceptance (#63): every parse is a reusable job item; a fresh
    `extract -d` runs a standalone parse first; repeat runs reuse it."""
    assert "reusable job item" in SKILL
    assert "standalone parse first" in SKILL
    assert "bills exactly once" in SKILL


def test_skill_teaches_pending_resume_and_json():
    assert "Always pass `--json`" in SKILL
    assert "same command" in SKILL  # the resume gesture
    assert '"status": "pending"' in SKILL
