# The schema gap is bridged in the skill layer, not in the CLI

## Context

The workflow is parse → schema → extract, and the middle step is the
user's: author a JSON Schema, then pass it to `extract --schema`.
Hand-authoring means guessing field names, running an extraction,
eyeballing the output, and repeating — with no measurement of whether it
got better. The build-schema skill (the `landingai-ade` plugin of
[landing-ai/claude-skills](https://github.com/landing-ai/claude-skills),
mirrored from `schema-loop` where it is benchmark-gated) already does
this properly: an AI-agent session drives this very CLI through measured
iteration — real extractions, reviewer-confirmed ground truth,
negotiated per-field accuracy targets — and delivers the final schema at
`<workspace>/deliverable/schema.json`. The two just didn't know about
each other.

A CLI-side bridge was designed and prototyped (a `schema build` command
plus a post-parse offer that launches an agent CLI as a foreground
subprocess). It was rejected: it grows the command surface, adds agent
adapters, config keys, and terminal-gating logic to a CLI whose posture
is deterministic plumbing, and it duplicates a doorway that the primary
callers — agents — never take, since an agent must not be handed an
interactive subprocess.

## Decision

The bridge lives in the skill layer. The root SKILL.md — the agent
contract shipped with this repo — teaches the integration: when the
user needs a schema authored, improved, or validated, the agent driving
this CLI invokes the **build-schema skill** directly (installing or
cloning it if absent), then feeds the delivered
`deliverable/schema.json` to `ade extract`.

The CLI itself is unchanged: no new commands, no menus, no config keys,
no subprocess launching, no agent detection. `parse`, `extract`, and the
whole machine contract stay byte-identical.

SKILL.md also carries the cost disclosure the agent must surface before
starting a run: the skill validates every schema version with a real
extraction, so a converging loop bills roughly one extraction per
document per iteration — and the final extract is normally a free cache
hit, because the loop's last iteration already ran it.

## Consequences

- Agents get the integration everywhere the skill travels, with zero
  new CLI surface to gate, test, or keep out of scripts and CI.
- ade never fetches, pins, or updates the build-schema skill — no new
  network surface, no version coupling. Skill acquisition is the
  agent's job, as SKILL.md instructs.
- Humans without an agent session keep the status quo: hand-author a
  schema or start an agent session themselves. If a human-facing
  doorway is ever wanted, it is a separate decision that must revisit
  the rejection above.
- The deliverable contract (`deliverable/schema.json`) is the one seam
  this repo trusts from the skill; if the skill's output layout ever
  changes, SKILL.md is the single place to follow it.
