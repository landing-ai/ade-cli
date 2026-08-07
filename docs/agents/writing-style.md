# Writing style

Standards for every string a user or an agent reads: help text and
docstrings (which become `ade help` and the hosted CLI reference), the
README, and SKILL.md.

## Prose standards

- Second person ("you"), active voice, direct language.
- No promotional language. Describe what a thing does.
- No idioms. Plain wording wins.
- Cite what a reader outside this repo can open: public docs, `ade help`
  topics, and shipped flags.
- Leave out issue numbers (enforced), internal URLs, and anything that
  resolves only inside LandingAI.
- Test every code example before it lands. Fence README and SKILL.md
  code blocks with a language tag.
- Error and status messages state what happened and what to do next.

## Inline code is always backticked (enforced)

Wrap flags (`--api-key`), commands (`ade update`), paths
(`~/.ade/credentials.json`), environment variables (`ADE_ENV`), and
literal values (`priority`) in backticks wherever they appear in prose.

Why: the hosted docs generate the CLI reference from `help --json` and
render a bare `--flag` as an em dash plus the flag name; agents need
unambiguous literal boundaries; and terminal output prints backticks
literally, which is already this CLI's voice.

`tests/test_help_style.py` enforces the flag rule across the help
surface's prose fields. Pre-formatted blocks are exempt: topic bodies
and usage lines are terminal layouts and copy-pasteable commands, so
leave them bare.

## Relationship to the docs repo

The docs repo (landing-ai/docs) has stricter rules for hosted pages.
This file governs text that lives in this repo; the docs repo's
generator is the compatibility layer between the two.
