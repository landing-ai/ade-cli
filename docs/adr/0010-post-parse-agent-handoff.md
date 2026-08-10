# The schema gap is bridged by handing off to an agent CLI, not by ade authoring schemas

## Context

The workflow is parse → schema → extract, and the middle step was entirely
the user's: hand-author a JSON Schema, then pass it to `extract --schema`.
The build-schema skill (the `landingai-ade` plugin of
[landing-ai/claude-skills](https://github.com/landing-ai/claude-skills),
mirrored from `schema-loop` where it is benchmark-gated) already automates
that step properly — an AI-agent session drives this very CLI through
measured iteration (real extractions, reviewer-built ground truth,
converged-or-blocked stops) and delivers the final schema at
`<workspace>/deliverable/schema.json`. The CLI and the skill each knew
nothing of the other, so the natural flow — parse, build the schema,
extract — required the user to know the skill exists and wire it up
themselves.

## Decision

ade bridges to the skill by **launching an agent CLI as a foreground
subprocess**, in two entry points sharing one body (`schema_build.py`):

- **`ade schema build JOB_ITEM_ID`** — the standalone command, usable on
  any stored parse.
- **A post-parse offer** — after a parse summary (fresh or cached hit) on
  a real terminal, an arrow-key menu (ADR-0002's pattern) asks whether to
  build a schema now. **Bare Enter exits**: the default preserves the old
  behavior keystroke-for-keystroke, and the summary's `next:` line remains
  the machine-mode teaching.

ade collects only what it already knows plus the extraction **intent**
(and optional existing-schema / ground-truth files); per-field accuracy
targets are deliberately *not* collected — the skill's own intake
negotiates them in conversation. The seeded prompt carries the full parse
job item id, source, workspace (`<ade-home>/schema-runs/<slug>-<date>/`
by default), and skill-acquisition instructions. When the session ends,
ade looks for `deliverable/schema.json`, names it, and offers to run the
extract — a re-exec of this CLI (`update.reexec_argv`, shared with the
viewer daemons), which is mostly a free cache hit since the skill's final
iteration already ran it.

**Agent adapters, not one agent.** A small table (`agents.py`) knows
`claude` (Claude Code — triggers the skill natively via the plugin) and
`codex`; detection is a PATH lookup through the injected `which` port. A
menu choice is remembered as `agent.default` in `config.json`; a custom
`agent.command` entry launches any other CLI. Every adapter receives the
seeded prompt as its final positional argument.

**ade stays network-free here.** It never fetches, pins, or updates the
skill: non-native agents are told to use a local copy or shallow-clone
the marketplace repo themselves. A missing agent CLI fails legibly with
install instructions (non-fatally on the offer path — the parse
succeeded).

**Gating.** The offer fires only when stdin, stdout, and stderr are all
terminals, never under `--json`/`--id-only`, never inside an agent host
or CI (surface.py detection — an agent driving `ade parse` must never
meet a menu), and `schema_prompt: false` silences it. The standalone
command requires a real terminal even under `--json` (the child session
inherits the streams; a piped stdout would be corrupted by its TUI) and
then requires `--intent`, resolves the agent without a menu, and never
auto-runs the extract.

## Consequences

- The interactive flow gains a doorway the machine contract cannot see:
  stdout stays payload-only, all prompts ride stderr, and every existing
  `--json` consumer is byte-identical.
- Subprocess launch and PATH lookup are ports (`run_agent`, `which`), so
  the suite tests the whole handoff offline — no real process ever runs.
- Agents driving ade should invoke the build-schema skill directly; the
  root SKILL.md says so. `ade schema build` is for humans at terminals.
- The deliverable contract (`deliverable/schema.json`) is the one seam
  ade trusts from the skill; if the skill's output layout ever changes,
  this is the single place to follow it.
