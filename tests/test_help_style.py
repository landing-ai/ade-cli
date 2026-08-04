"""Style guard for the help surface: inline code is always backticked.

A bare ``--flag`` in help prose is ambiguous for agents and renders as
an em dash plus the flag name on the hosted docs, which generate the
CLI reference from ``help --json``. See docs/agents/writing-style.md.

Scope: every prose field in the help surface. Topic bodies are exempt:
they are pre-formatted terminal layouts (aligned flag columns, literal
example commands) where backticks would corrupt the rendering. Other
inline-code kinds (paths, commands, env vars) stay convention; a test
cannot reliably tell a path from prose, but a long flag is unmistakable.
"""

from __future__ import annotations

import json
import re

import pytest

# A long flag in prose that is not already inside a backtick span.
CODE_SPAN = re.compile(r"`[^`]*`")
BARE_FLAG = re.compile(r"(?<![\w`-])--[a-z][a-z0-9-]*")


@pytest.fixture
def reference(cli) -> dict:
    result = cli.invoke("help", "--json")
    assert result.exit_code == 0
    return json.loads(result.stdout)


def _prose_strings(reference: dict):
    """Yield (locator, text) for every prose string in the help surface."""
    yield "description", reference["description"]
    for convention in reference["conventions"]:
        yield f"conventions[{convention['name']}]", convention["rule"]
    for command in reference["commands"]:
        where = f"commands[{command['name']}]"
        yield f"{where}.summary", command["summary"]
        for argument in command["arguments"]:
            yield f"{where}.arguments[{argument['name']}]", argument["help"]
        for flag in command["flags"]:
            yield f"{where}.flags[{flag['flags']}]", flag["help"]
        for key in command.get("result", {}).get("keys", []):
            yield f"{where}.result[{key['key']}]", key["what"]
        note = command.get("result", {}).get("note")
        if note:
            yield f"{where}.result.note", note
    # topics[].body is deliberately absent: pre-formatted terminal text.
    for state in reference["exit_states"]:
        yield f"exit_states[{state['name']}]", state["meaning"]
    for entry in reference["store"]["layout"]:
        yield f"store[{entry['path']}]", entry["what"]


def test_flags_in_help_prose_are_backticked(reference):
    """Every --flag mentioned in help prose is wrapped in backticks."""
    offenders = []
    for locator, text in _prose_strings(reference):
        stripped = CODE_SPAN.sub("", text)
        for match in BARE_FLAG.findall(stripped):
            offenders.append(f"{locator}: bare {match}")
    assert not offenders, (
        "Bare flags in help prose; wrap them in backticks "
        "(docs/agents/writing-style.md):\n" + "\n".join(offenders)
    )
