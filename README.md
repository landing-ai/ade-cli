# ade-cli

[![Release](https://img.shields.io/github/v/release/landing-ai/ade-cli)](https://github.com/landing-ai/ade-cli/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Agentic Document Extraction (ADE), from your terminal.** Run `ade parse` to
turn documents with tables, figures, and charts into grounded Markdown and
elements, and `ade extract` to pull schema-shaped fields with page-and-box
evidence. Both save to a local store at `~/.ade`, so re-running an identical
command consumes no credits.

**[CLI documentation](https://docs.landing.ai/cli/quickstart)** ·
**[Playground](https://ade.landing.ai)** ·
**[Get an API key](https://ade.landing.ai/settings/api-key)**

## Install

Each release ships self-contained binaries for macOS, Linux, and Windows
(arm64 and x86_64). No Python or `uv` needed.

**macOS / Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/landing-ai/ade-cli/main/scripts/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/landing-ai/ade-cli/main/scripts/install.ps1 | iex
```

**Windows (CMD)**

```bat
curl -fsSL https://raw.githubusercontent.com/landing-ai/ade-cli/main/scripts/install.cmd -o install.cmd && install.cmd && del install.cmd
```

The app lands in `~/.ade/bin`, and the installer symlinks `ade` into
`~/.local/bin`, which is on the PATH in most setups. Set `ADE_HOME` to move the
app and the store, `ADE_CLI_VERSION` to pin a version, and
`ADE_CLI_INSTALL_DIR` to change the destination.

To uninstall, remove `~/.ade/bin` and the `~/.local/bin/ade` symlink. Never run
`rm -rf ~/.ade`: the rest of that directory holds results that already consumed
credits.

Run `ade update` to self-update, with `--yes` to skip the confirmation. For a
`uv` or `pipx` install it points at `uv tool upgrade ade-cli` instead. The CLI
mentions new releases on stderr once a day. Set `ADE_NO_UPDATE_CHECK=1` to turn
that off.

To develop or install from source, see [CONTRIBUTING.md](CONTRIBUTING.md).

## First run

Log in once per machine, parse a document, then extract the fields you want
from it. The parse summary prints the job item ID that `extract` takes.

```sh
ade login
ade parse -d invoice.pdf
ade extract JOB_ITEM_ID --schema schema.json
```

The [CLI Quickstart](https://docs.landing.ai/cli/quickstart) walks through the
same three steps with a sample document and a sample schema, and ends in the
viewer that shows where each extracted value came from.

## Commands

Every command takes `--json` for one stable JSON object on stdout. Run
`ade help COMMAND` for its flags and result shape, or see the
[CLI Reference](https://docs.landing.ai/cli/reference).

| command | what it does |
|---|---|
| `ade parse` | Parse a document into Markdown, elements, and grounding |
| `ade extract` | Pull schema-shaped fields from a parse, with per-field evidence |
| `ade find` | Search a parse's elements locally, with no API call |
| `ade crop` | Save an element's region of the page as a PNG |
| `ade view` | Build and open a self-contained HTML viewer for a job item |
| `ade history list` | List stored job items, newest first |
| `ade history clear` | Delete stored job items |
| `ade login` / `ade auth login` | Log in with your browser or an API key |
| `ade auth status` | Show how you are authenticated and where |
| `ade auth org list` / `switch` / `clear` | Choose which organization your runs act in |
| `ade logout` / `ade auth logout` | Log out of one environment, or all of them |
| `ade version` | Print the version and install mode |
| `ade update` | Self-update to the latest release |
| `ade help` | Print the whole command surface, including `--json` result shapes |

The documentation covers what these tables cannot: how job items work and why
re-runs are free ([CLI Concepts](https://docs.landing.ai/cli/concepts)),
[parsing](https://docs.landing.ai/cli/parse) and
[extraction](https://docs.landing.ai/cli/extract) in depth,
[searching and cropping evidence](https://docs.landing.ai/cli/find-crop-view),
[authentication and organizations](https://docs.landing.ai/cli/authentication),
and [scripting the CLI](https://docs.landing.ai/cli/scripting-automation).

## For agents

Agents are first-class callers. Running `ade help --json` returns the entire
shipped surface in one call: every command, flag, result shape, topic, exit
state, and the store layout. Every command then takes `--json` and emits one
stable object whose shape is published per verb, and the full result is always
on stdout rather than only in a store file.
[`SKILL.md`](SKILL.md) ships the agent contract: the loop, the conventions, and
the sharp edges.

Non-interactive shells such as CI, cron, and agent harnesses source no rc file,
so a PATH entry your login shell has may be missing. Call the absolute path
`~/.ade/bin/ade`, or export `PATH="$HOME/.ade/bin:$PATH"` inside the job. For
credentials, set `ADE_API_KEY`.

## Project history

This repository was **`agentic-doc`**, the original Agentic Document Extraction
SDK. That SDK is preserved unchanged, and no longer developed, on the
[`legacy`](https://github.com/landing-ai/ade-cli/tree/legacy) branch. Since
2026-07-31 this repository ships the ADE CLI.

## License

[Apache-2.0](LICENSE).
