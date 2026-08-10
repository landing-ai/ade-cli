# ade-cli

[![Release](https://img.shields.io/github/v/release/landing-ai/ade-cli)](https://github.com/landing-ai/ade-cli/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Agentic Document Extraction (ADE), from your terminal.** The `ade` command
drives LandingAI's ADE v2 APIs: `parse` turns visually complex documents
(tables, figures, charts) into grounded Markdown and elements, and `extract`
pulls schema-shaped fields with page-and-box evidence. Everything a run
produces is saved in a local store at `~/.ade` and served from there, so
re-running an identical command consumes no credits.

**[CLI documentation](https://docs.landing.ai/cli/quickstart)** ·
**[Playground](https://ade.landing.ai)** ·
**[Get an API key](https://ade.landing.ai/settings/api-key)**

## Install

Each release ships self-contained binaries for macOS, Linux, and Windows
(arm64 and x86_64). No Python and no `uv` are needed. The installers detect
your platform, verify the checksum, and put `ade` on your machine.

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

The app lands in `~/.ade/bin` (`%USERPROFILE%\.ade\bin` on Windows): the binary
plus an `_internal/` directory holding its bundled libraries. That sits inside
the CLI's own home, next to the store, and `ADE_HOME` moves both. The installer
also symlinks `ade` into `~/.local/bin`, which is on the PATH in most setups,
so no rc-file edit is needed. It verifies the link runs and never overwrites a
real file at that name.

To pin a version, set `ADE_CLI_VERSION=1.0.5`. To change the destination, set
`ADE_CLI_INSTALL_DIR`. To uninstall, remove `~/.ade/bin` and the
`~/.local/bin/ade` symlink. Never run `rm -rf ~/.ade`: the rest of that
directory is your local record of parse and extract results that already
consumed credits.

Run `ade update` to check the release channel and self-update after a
confirmation prompt, which `--yes` skips. A `uv` or `pipx` install is never
touched in place; `update` points at `uv tool upgrade ade-cli` instead, and
`ade version` reports which kind of install you have. The CLI also checks
quietly in the background at most once a day and mentions a newer release on
stderr. Set `ADE_NO_UPDATE_CHECK=1` to turn that off.

Developing, or installing from source? See [CONTRIBUTING.md](CONTRIBUTING.md).

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
