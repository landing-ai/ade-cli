"""Agent-CLI adapters for the build-schema handoff (schema_build.py).

The build-schema skill runs inside an AI-agent session, not inside this
process — this module knows which agent CLIs can host one, how to find
them on PATH, and what seeded prompt hands the skill everything the CLI
already knows (the parsed job item, the intent, the workspace) without
collecting what the skill negotiates itself (accuracy targets). Pure
functions over injected lookups; no typer, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

# The public marketplace mirror the seeded prompt names for agents that
# cannot trigger the skill natively; ade never fetches it itself.
SKILLS_REPO_URL = "https://github.com/landing-ai/claude-skills"
SKILL_PATH = "plugins/landingai-ade/skills/build-schema/SKILL.md"
# The skill's output contract: the final schema, byte-identical to the
# version its last iteration actually ran.
DELIVERABLE = Path("deliverable") / "schema.json"


@dataclass(frozen=True)
class Agent:
    """One launchable agent CLI. The seeded prompt is always appended as
    the final positional argument — every supported CLI opens an
    interactive session seeded with its positional prompt."""

    name: str
    command: tuple[str, ...]
    install_hint: str
    # Whether the landingai-ade plugin can trigger the skill natively;
    # other agents get the clone-and-follow instructions instead.
    native_skill: bool = False


KNOWN_AGENTS: tuple[Agent, ...] = (
    Agent(
        "claude",
        ("claude",),
        "npm install -g @anthropic-ai/claude-code",
        native_skill=True,
    ),
    Agent("codex", ("codex",), "npm install -g @openai/codex"),
)


def custom_agent(config: Mapping) -> Agent | None:
    """The config.json escape hatch: ``agent.command`` names any CLI not
    in the adapter table. Malformed entries read as absent — a config
    typo must degrade to detection, never crash a parse."""
    entry = config.get("agent")
    if not isinstance(entry, Mapping):
        return None
    command = entry.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(token, str) and token for token in command)
    ):
        return None
    name = entry.get("name")
    return Agent(
        name if isinstance(name, str) and name else command[0],
        tuple(command),
        install_hint="",
    )


def detect(which: Callable[[str], str | None], config: Mapping) -> list[Agent]:
    """The launchable agents, best first. A resolvable ``agent.command``
    override wins outright; otherwise every known agent on PATH, with a
    remembered ``agent.default`` collapsing the list to that one choice
    so the menu asks only once."""
    custom = custom_agent(config)
    if custom is not None and which(custom.command[0]):
        return [custom]
    found = [agent for agent in KNOWN_AGENTS if which(agent.command[0])]
    entry = config.get("agent")
    default = entry.get("default") if isinstance(entry, Mapping) else None
    for agent in found:
        if agent.name == default:
            return [agent]
    return found


def install_message() -> str:
    """The no-agent-found remediation: name the known CLIs, their install
    commands, and the config escape hatch for anything else."""
    lines = ["no AI agent CLI found on PATH; install one to build schemas:"]
    for agent in KNOWN_AGENTS:
        lines.append(f"  {agent.name}:  {agent.install_hint}")
    lines.append(
        '  or point config.json at another agent: {"agent": '
        '{"name": "...", "command": ["...", "..."]}}'
    )
    return "\n".join(lines)


def seeded_prompt(
    *,
    agent: Agent,
    intent: str,
    job_item_id: str,
    source: str,
    store_dir: Path,
    workspace: Path,
    environment: str | None = None,
    existing_schema: Path | None = None,
    ground_truth: Path | None = None,
) -> str:
    """The one message the agent session starts from. It carries what ade
    already knows (identifiers, paths, the intent) and how to acquire the
    skill; it deliberately omits accuracy targets — the skill's own
    intake negotiates those with the user in conversation."""
    if agent.native_skill:
        skill_lines = [
            "Invoke the build-schema skill (from the landingai-ade plugin of "
            f"{SKILLS_REPO_URL}) and follow it exactly. If the plugin is not "
            "installed, acquire the skill first:",
        ]
    else:
        skill_lines = [
            "Follow the ADE build-schema skill exactly. Use a locally "
            "installed copy if one exists; otherwise acquire it first:",
        ]
    skill_lines += [
        f"  git clone --depth 1 {SKILLS_REPO_URL} {workspace / '.claude-skills'}",
        f"  then follow {SKILL_PATH} from that clone.",
    ]
    inputs = [
        f"- Extraction intent: {intent}",
        f"- Parse job item id: {job_item_id} — the document is already "
        f"parsed; artifacts live in {store_dir} (parse.json, parse.md, "
        "elements.json). Reuse that parse: never re-parse or pass --force.",
        f"- Source document: {source}",
    ]
    if environment is not None:
        inputs.append(f"- ADE environment: {environment}")
    inputs.append(f"- Workspace: use exactly {workspace} as the skill workspace.")
    if existing_schema is not None:
        inputs.append(
            f"- Existing schema (iteration-1 baseline): {existing_schema}"
        )
    if ground_truth is not None:
        inputs.append(f"- Golden ground truth: {ground_truth}")
    return "\n".join(
        [
            "Build an extraction schema with the ADE build-schema skill for "
            "a document already parsed locally by the ade CLI.",
            "",
            *skill_lines,
            "",
            "Inputs for the skill:",
            *inputs,
            "",
            "The ade CLI is on PATH. Check `ade auth status --json` before "
            "starting and pass --json to every ade command.",
            "The skill negotiates per-field accuracy targets in conversation "
            "— that was deliberately not collected here; do it with the user.",
            f"Deliver the final schema at {workspace / DELIVERABLE}.",
        ]
    )
