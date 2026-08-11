"""``schema build`` — the handoff into the build-schema agent skill.

The command's whole job is orchestration: find an agent CLI, seed it with
what ade already knows, account for the deliverable, offer the extract.
Every test drives the CLI seam with the subprocess and PATH ports faked —
the suite never launches a real agent (or any process at all).
"""

import json
import re
import sys
from pathlib import Path

import pytest

from ade_cli import agents

from conftest import RecordedRuns
from extract_fixtures import SCHEMA, completed_extract_job
from parse_fixtures import JOB_ID, completed_job

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}
DOC_BYTES = b"%PDF-1.4 fake invoice bytes"

CLAUDE = {"claude": "/usr/local/bin/claude"}
BOTH = {"claude": "/usr/local/bin/claude", "codex": "/usr/local/bin/codex"}


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(DOC_BYTES)
    return path


@pytest.fixture
def tty(cli):
    """The command requires a real terminal on all three streams; the
    handoff tests opt the harness into one."""
    cli.stdin_tty = cli.stdout_tty = cli.stderr_tty = True
    return cli


def parse_doc(cli, document):
    """Seed a completed parse job item; returns its job item id."""
    cli.transport.respond(202, {"job_id": JOB_ID})
    cli.transport.respond(200, completed_job())
    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


def deliver_schema(argv, cwd):
    """RecordedRuns side effect standing in for the skill: the agent
    session 'ran' and left the five-artifact deliverable's schema."""
    (cwd / "deliverable").mkdir(parents=True, exist_ok=True)
    (cwd / "deliverable" / "schema.json").write_text(json.dumps(SCHEMA))
    return 0


# ---------------------------------------------------------------- agents.py


def prompt_for(agent, **overrides):
    defaults = dict(
        agent=agent,
        intent="invoice totals and line items",
        job_item_id="a" * 16,
        source="/docs/invoice.pdf",
        store_dir=Path("/home/u/.ade/jobs") / ("a" * 16),
        workspace=Path("/home/u/.ade/schema-runs/invoice-2026-08-10"),
    )
    defaults.update(overrides)
    return agents.seeded_prompt(**defaults)


def test_seeded_prompt_carries_the_full_item_and_the_rules():
    prompt = prompt_for(agents.KNOWN_AGENTS[0])
    assert "a" * 16 in prompt  # the full id, never a prefix
    assert "never re-parse or pass --force" in prompt
    assert "ade auth status --json" in prompt
    # Targets are the skill's to negotiate — the prompt must say so, not
    # carry numbers.
    assert "accuracy targets" in prompt and "80" not in prompt
    assert "deliverable" in prompt and "schema.json" in prompt


def test_seeded_prompt_skill_acquisition_differs_by_agent():
    claude, codex = agents.KNOWN_AGENTS
    assert "landingai-ade plugin" in prompt_for(claude)
    for prompt in (prompt_for(claude), prompt_for(codex)):
        # Both carry the clone fallback (claude may lack the plugin too).
        assert "git clone --depth 1 https://github.com/landing-ai/claude-skills" in prompt
        assert "plugins/landingai-ade/skills/build-schema/SKILL.md" in prompt


def test_seeded_prompt_optional_lines_appear_only_when_given(tmp_path):
    bare = prompt_for(agents.KNOWN_AGENTS[0])
    assert "ADE environment" not in bare
    assert "Existing schema" not in bare and "ground truth" not in bare
    full = prompt_for(
        agents.KNOWN_AGENTS[0],
        environment="staging",
        existing_schema=tmp_path / "old.json",
        ground_truth=tmp_path / "golden.csv",
    )
    assert "ADE environment: staging" in full
    assert "old.json" in full and "golden.csv" in full


def test_detect_orders_and_collapses():
    which = lambda name: BOTH.get(name)  # noqa: E731
    assert [a.name for a in agents.detect(which, {})] == ["claude", "codex"]
    assert [a.name for a in agents.detect(which, {"agent": {"default": "codex"}})] == [
        "codex"
    ]
    # A custom command wins outright when resolvable, degrades when not.
    custom = {"agent": {"name": "aider", "command": ["aider", "--message"]}}
    which_custom = lambda name: {"aider": "/bin/aider", **BOTH}.get(name)  # noqa: E731
    assert [a.name for a in agents.detect(which_custom, custom)] == ["aider"]
    assert [a.name for a in agents.detect(which, custom)] == ["claude", "codex"]
    # Malformed config entries read as absent, never crash.
    assert agents.detect(lambda name: None, {"agent": {"command": "aider"}}) == []


# ---------------------------------------------------------- ade schema build


def test_build_requires_a_real_terminal(cli, document):
    item_id = parse_doc(cli, document)
    result = cli.invoke("schema", "build", item_id, "--json", which=CLAUDE)
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "no_terminal"


def test_build_without_an_agent_fails_with_install_instructions(tty, document):
    item_id = parse_doc(tty, document)
    result = tty.invoke("schema", "build", item_id, "--intent", "totals")
    assert result.exit_code == 1
    assert "no AI agent CLI found" in result.stdout
    assert "@anthropic-ai/claude-code" in result.stdout
    assert "@openai/codex" in result.stdout


def test_build_launches_the_agent_and_offers_the_extract(tty, document):
    item_id = parse_doc(tty, document)
    runs = RecordedRuns([deliver_schema, 0])  # the session, then the extract

    result = tty.invoke(
        "schema", "build", item_id, "--intent", "invoice totals",
        which=CLAUDE, run=runs, input="\n\ny\n",  # skip, skip, extract yes
    )

    assert result.exit_code == 0, result.stdout
    session, extract = runs.calls
    argv, cwd = session
    assert argv[0] == "claude" and len(argv) == 2
    assert item_id in argv[1] and "invoice totals" in argv[1]
    assert cwd is not None and re.fullmatch(
        rf"invoice-{item_id[:8]}-\d{{4}}-\d{{2}}-\d{{2}}", cwd.name
    ), cwd
    assert cwd.parent == tty.home / "schema-runs"
    schema_path = cwd / "deliverable" / "schema.json"
    assert str(schema_path) in result.stdout  # the summary names the schema
    # The extract is a re-exec of this CLI against the same item.
    assert extract[0] == [
        sys.executable, "-m", "ade_cli",
        "extract", item_id, "--schema", str(schema_path),
    ]


def test_build_extract_declined_is_a_clean_stop(tty, document):
    item_id = parse_doc(tty, document)
    runs = RecordedRuns([deliver_schema])

    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=CLAUDE, run=runs, input="\n\nn\n",
    )

    assert result.exit_code == 0, result.stdout
    assert len(runs.calls) == 1
    assert "next:" in result.stdout and "ade extract" in result.stdout


def test_build_json_contract(tty, document):
    item_id = parse_doc(tty, document)
    # --json cannot prompt: intent is required up front.
    missing = tty.invoke("schema", "build", item_id, "--json", which=CLAUDE)
    assert missing.exit_code == 2
    assert json.loads(missing.stdout)["error"] == "intent_required"

    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=runs,
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "schema_built"
    assert payload["job_item_id"] == item_id
    assert payload["agent"] == "claude"
    assert payload["schema"].endswith("schema.json")
    assert payload["extracted"] is False
    assert payload["extract_hint"].startswith("ade extract ")
    assert len(runs.calls) == 1  # --json never auto-runs the extract


def test_build_no_deliverable_names_the_workspace(tty, document):
    item_id = parse_doc(tty, document)
    runs = RecordedRuns([3])  # the session ends without writing anything

    result = tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=runs,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "no_deliverable"
    assert payload["agent_exit_code"] == 3
    assert tty.home / "schema-runs" in Path(payload["workspace"]).parents


def test_build_menu_remembers_the_choice(tty, document):
    item_id = parse_doc(tty, document)

    first = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=BOTH, run=RecordedRuns([deliver_schema]),
        keys=["2", "\r"], input="\n\nn\n",  # pick codex on the arrow menu
    )
    assert first.exit_code == 0, first.stdout
    config = json.loads((tty.home / "config.json").read_text())
    assert config["agent"]["default"] == "codex"

    # The remembered default collapses detection: no menu (keys would be
    # an unscripted read), straight to codex.
    runs = RecordedRuns([deliver_schema])
    second = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=BOTH, run=runs, input="\n\nn\n",
    )
    assert second.exit_code == 0, second.stdout
    assert runs.calls[0][0][0] == "codex"


def test_build_agent_flag_beats_the_remembered_default(tty, document):
    item_id = parse_doc(tty, document)
    (tty.home).mkdir(parents=True, exist_ok=True)
    (tty.home / "config.json").write_text(
        json.dumps({"agent": {"default": "codex"}})
    )
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals", "--agent", "claude",
        which=BOTH, run=runs, input="\n\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert runs.calls[0][0][0] == "claude"

    # And an --agent that is not on PATH is a legible failure.
    missing = tty.invoke(
        "schema", "build", item_id, "--intent", "totals", "--agent", "claude",
        which={"codex": "/usr/local/bin/codex"},
    )
    assert missing.exit_code == 1
    assert "not available" in missing.stdout


def test_build_custom_agent_command(tty, document):
    item_id = parse_doc(tty, document)
    (tty.home).mkdir(parents=True, exist_ok=True)
    (tty.home / "config.json").write_text(
        json.dumps({"agent": {"name": "aider", "command": ["aider", "--message"]}})
    )
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which={"aider": "/bin/aider"}, run=runs, input="\n\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    argv, _cwd = runs.calls[0]
    assert argv[:2] == ["aider", "--message"] and item_id in argv[2]


def test_build_refuses_an_extract_item(tty, document, tmp_path):
    parse_id = parse_doc(tty, document)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps(SCHEMA))
    tty.transport.respond(202, {"job_id": "extract-0001"})
    tty.transport.respond(200, completed_extract_job())
    extracted = tty.invoke(
        "extract", parse_id, "--schema", str(schema_file), "--json", env=AUTH_ENV
    )
    assert extracted.exit_code == 0, extracted.stdout
    extract_id = json.loads(extracted.stdout)["job_item_id"]

    result = tty.invoke(
        "schema", "build", extract_id, "--json", "--intent", "totals", which=CLAUDE
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "not_a_parse_item"


# ------------------------------------------------- workspace & prompt details


def test_workspace_slug_comes_from_the_source_and_is_reused(tty, document):
    """The default workspace names the engagement after the document, and
    a second run lands in the same directory so the skill resumes rather
    than starting a fresh loop."""
    item_id = parse_doc(tty, document)
    first = RecordedRuns([deliver_schema])
    tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=first,
    )
    second = RecordedRuns([deliver_schema])
    tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=second,
    )
    assert first.calls[0][1] == second.calls[0][1]
    assert first.calls[0][1].name.startswith("invoice-")


def test_same_filename_different_documents_get_distinct_workspaces(tty, tmp_path):
    """Filename stem + date alone would collide two different documents
    both named invoice.pdf — and the second run would report the first
    one's stale deliverable as its own. The item id (a content hash) in
    the default workspace name keeps them apart."""
    workspaces = []
    for n, content in enumerate((b"%PDF-1.4 doc one", b"%PDF-1.4 doc two")):
        doc_dir = tmp_path / f"client-{n}"
        doc_dir.mkdir()
        doc = doc_dir / "invoice.pdf"
        doc.write_bytes(content)
        tty.transport.respond(202, {"job_id": f"job-{n}"})
        tty.transport.respond(200, completed_job(job_id=f"job-{n}"))
        parsed = tty.invoke("parse", "-d", str(doc), "--json", env=AUTH_ENV)
        assert parsed.exit_code == 0, parsed.stdout
        item_id = json.loads(parsed.stdout)["job_item_id"]
        runs = RecordedRuns([deliver_schema])
        result = tty.invoke(
            "schema", "build", item_id, "--json", "--intent", "totals",
            which=CLAUDE, run=runs,
        )
        assert result.exit_code == 0, result.stdout
        workspaces.append(runs.calls[0][1])
    assert workspaces[0] != workspaces[1]


def test_explicit_workspace_is_honored(tty, document, tmp_path):
    item_id = parse_doc(tty, document)
    workspace = tmp_path / "engagement"
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        "--workspace", str(workspace), which=CLAUDE, run=runs,
    )
    assert result.exit_code == 0, result.stdout
    assert runs.calls[0][1] == workspace
    assert json.loads(result.stdout)["workspace"] == str(workspace)


def test_url_sourced_parse_still_yields_a_usable_slug(tty):
    """A URL source has no filesystem stem to borrow; the workspace name
    must still be a clean directory (query strings and slashes stripped)."""
    tty.transport.respond(202, {"job_id": JOB_ID})
    tty.transport.respond(200, completed_job())
    parsed = tty.invoke(
        "parse", "--document-url", "https://example.com/docs/Q3%20Report.pdf?sig=abc",
        "--json", env=AUTH_ENV,
    )
    assert parsed.exit_code == 0, parsed.stdout
    item_id = json.loads(parsed.stdout)["job_item_id"]
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=runs,
    )
    assert result.exit_code == 0, result.stdout
    name = runs.calls[0][1].name
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{8}-\d{4}-\d{2}-\d{2}", name), name


def test_flags_feed_the_prompt_without_prompting(tty, document, tmp_path):
    """--existing-schema and --ground-truth skip their prompts and reach
    the agent as the skill's baseline and golden set."""
    item_id = parse_doc(tty, document)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(SCHEMA))
    golden = tmp_path / "golden.csv"
    golden.write_text("doc_id,total\ninvoice.pdf,42\n")
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        "--existing-schema", str(baseline), "--ground-truth", str(golden),
        which=CLAUDE, run=runs, input="n\n",  # only the extract confirm remains
    )
    assert result.exit_code == 0, result.stdout
    prompt = runs.calls[0][0][1]
    assert str(baseline) in prompt and str(golden) in prompt


def test_optional_path_typo_reprompts_instead_of_dropping_it(tty, document, tmp_path):
    item_id = parse_doc(tty, document)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(SCHEMA))
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=CLAUDE, run=runs,
        input=f"/no/such/schema.json\n{baseline}\n\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "No such file" in result.stderr
    assert str(baseline) in runs.calls[0][0][1]


def test_prompt_names_a_non_default_environment(tty, document):
    """The skill must run its extractions in the parse item's own
    environment; production stays unstated (the beaten path)."""
    tty.transport.respond(202, {"job_id": JOB_ID})
    tty.transport.respond(200, completed_job())
    staged = tty.invoke(
        "parse", "-d", str(document), "--env", "staging", "--json", env=AUTH_ENV
    )
    assert staged.exit_code == 0, staged.stdout
    item_id = json.loads(staged.stdout)["job_item_id"]
    runs = RecordedRuns([deliver_schema])
    tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=runs,
    )
    assert "ADE environment: staging" in runs.calls[0][0][1]


# --------------------------------------------------------- failure & fallback


def test_agent_that_cannot_be_launched_fails_legibly(tty, document):
    item_id = parse_doc(tty, document)

    def explode(argv, cwd=None):
        raise OSError("Exec format error")

    result = tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=explode,
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "agent_launch_failed"
    assert "Exec format error" in payload["message"]


def test_extract_exit_code_propagates(tty, document):
    """The handoff ends as the extraction ended — a failed extract must
    not be reported as a successful build."""
    item_id = parse_doc(tty, document)
    runs = RecordedRuns([deliver_schema, 4])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=CLAUDE, run=runs, input="\n\ny\n",
    )
    assert result.exit_code == 4


def test_menu_falls_back_to_typed_input_when_raw_mode_fails(tty, document):
    """term.Unsupported (a terminal that lies about raw mode) must reach
    the numbered prompt, not abort the handoff."""
    item_id = parse_doc(tty, document)
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=BOTH, run=runs, keys=[OSError("no raw mode")],
        input="2\n\n\nn\n",  # typed agent choice, two skips, decline extract
    )
    assert result.exit_code == 0, result.stdout
    assert runs.calls[0][0][0] == "codex"


def test_dumb_terminal_uses_the_typed_menu(tty, document):
    item_id = parse_doc(tty, document)
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=BOTH, run=runs, env={"TERM": "dumb"},
        input="2\n\n\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert runs.calls[0][0][0] == "codex"


def test_unwritable_config_does_not_break_the_handoff(tty, document):
    """Remembering the menu choice is advisory — a config.json that is a
    directory (unwritable) must cost the memory, not the run."""
    item_id = parse_doc(tty, document)
    (tty.home).mkdir(parents=True, exist_ok=True)
    (tty.home / "config.json").mkdir()
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=BOTH, run=runs, keys=["1", "\r"], input="\n\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert runs.calls[0][0][0] == "claude"


def test_unknown_id_is_the_shared_error_shape(tty):
    result = tty.invoke(
        "schema", "build", "ffffffff", "--json", "--intent", "totals", which=CLAUDE
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "unknown_id"


def test_agent_ambiguous_under_json(tty, document):
    """--json cannot show a menu, so several agents is a usage error that
    names the flag that resolves it."""
    item_id = parse_doc(tty, document)
    result = tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals", which=BOTH
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "agent_ambiguous"
    assert sorted(payload["agents"]) == ["claude", "codex"]
    assert "--agent" in payload["message"]


def test_corrupt_config_costs_the_preference_not_the_handoff(tty, document):
    """config.json is hand-editable; invalid JSON there must not take the
    handoff down with it — the agent preference is the only thing lost."""
    item_id = parse_doc(tty, document)
    (tty.home).mkdir(parents=True, exist_ok=True)
    (tty.home / "config.json").write_text("{not valid json")
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--json", "--intent", "totals",
        which=CLAUDE, run=runs,
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["agent"] == "claude"


def test_credit_cost_is_stated_before_the_session_starts(tty, document):
    """The skill bills per iteration, not once — the user must learn that
    before the agent launches, not from the invoice."""
    item_id = parse_doc(tty, document)
    runs = RecordedRuns([deliver_schema])
    result = tty.invoke(
        "schema", "build", item_id, "--intent", "totals",
        which=CLAUDE, run=runs, input="\n\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "bills credits" in result.stderr
    assert "each iteration" in result.stderr


def test_help_warns_that_the_skill_bills_credits(cli):
    """Both help surfaces carry the cost: the command's own --help and
    the published result contract agents read."""
    scoped = cli.invoke("help", "schema build")
    assert scoped.exit_code == 0
    assert "Bills credits" in scoped.stdout

    reference = json.loads(cli.invoke("help", "--json").stdout)
    record = next(
        c for c in reference["commands"] if c["name"] == "schema build"
    )
    assert "BILLS CREDITS" in record["result"]["note"]
    workflow = next(t for t in reference["topics"] if t["name"] == "workflow")
    assert any("bills credits" in line for line in workflow["body"])
