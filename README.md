# ade-cli

[![Release](https://img.shields.io/github/v/release/landing-ai/ade-cli)](https://github.com/landing-ai/ade-cli/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Agentic Document Extraction (ADE), from your terminal.** The `ade` command drives
LandingAI's ADE v2 document APIs — `parse` turns visually complex documents
(tables, figures, charts) into grounded markdown and elements; `extract` pulls
schema-shaped fields with page-and-box evidence — and persists everything it
produces in a local store (`~/.ade`), serving every read from it.

**[Documentation](https://docs.landing.ai/)** ·
**[Playground](https://ade.landing.ai)** ·
**[Get an API key](https://ade.landing.ai/settings/api-key)**

## Install

Each release ships self-contained binaries for macOS, Linux, and Windows
(arm64 and x86_64 each) — no Python, no `uv`. The installers detect your
platform, verify the checksum, and put `ade` on your machine:

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

The app lands in `~/.ade/bin` (`%USERPROFILE%\.ade\bin` on Windows) — the
binary plus an `_internal/` dir with its bundled libraries — inside the
CLI's own home, next to the store; `ADE_HOME` moves both. The installer
also symlinks `ade` into `~/.local/bin` (creating it if needed — it's on
PATH in most setups, so no rc-file edit is needed) and verifies the link
runs; it never overwrites a real file at that name. Scripting `ade`? See
[Agents](#agents): a non-interactive shell sources no rc file, so use the
absolute path `~/.ade/bin/ade`. Pin a version with `ADE_CLI_VERSION=0.2.1`, change
the destination with `ADE_CLI_INSTALL_DIR`. To uninstall, remove
`~/.ade/bin` and the `~/.local/bin/ade` symlink — never
`rm -rf ~/.ade`, since the rest of that directory is your local store of
billed parse/extract results.
(Developing or installing from source? See
[CONTRIBUTING.md](CONTRIBUTING.md).)

Staying current is one command: `ade update` checks the release channel
and, for a binary install, replaces the app in place after verifying the
release checksum (`--yes` skips the confirmation). A `uv`/`pipx` install
is never touched — `update` points at `uv tool upgrade ade-cli` instead;
`ade version` reports which kind you have. The CLI also checks quietly
in the background at most once a day and mentions a newer release on
stderr; set `ADE_NO_UPDATE_CHECK=1` to turn that off.

## Auth

| command | what it does |
|---|---|
| `ade auth login` | choose a method: paste an API key, or sign in with your browser (OAuth) |
| `ade auth login --api-key KEY` | store an API key for production directly |
| `ade auth login --api-key -` | prompt for the key (hidden input) |
| `echo $KEY \| ade auth login` | headless: a piped key is the prompt, answered |
| `ade auth login --env eu` | log in to the EU region (no-op if it already holds a credential) |
| `ade auth status` | the resolved target + every other environment holding a credential |
| `ade auth logout` | log out of the resolved target |
| `ade auth logout --env eu` | log out of a specific environment |
| `ade auth logout --all` | log out of every environment |
| `ade login` | top-level alias of `ade auth login` — same flags, same behavior |
| `ade logout` | top-level alias of `ade auth logout` — same flags, same behavior |

Browser sign-in runs an OAuth flow against your LandingAI account and
stores refreshable tokens; nothing to copy. API keys come from your
[LandingAI account settings](https://ade.landing.ai/settings/api-key).
`login` verifies the key against the target environment before storing
it — a mistyped key fails right at login, not at your first `parse`.

**There is no "current environment" — the target is resolved fresh on every
command**: the `--env` flag, else the `ADE_ENV` variable, else `production`.
`export ADE_ENV=eu` gives one shell sticky EU while another shell works
production; nothing global changes, and `parse`/`extract`/`auth` all
follow the same rule, so a login and the verb after it can never disagree.
Credentials are stored per environment, because ADE credentials don't cross
environments:

| `--env` / `ADE_ENV` | endpoint |
|---|---|
| `production` (default) | `https://api.ade.landing.ai` |
| `eu` | `https://api.ade.eu-west-1.landing.ai` |

`login` ensures the target is authenticated: with a stored credential it says
so and does nothing; otherwise it acquires one (menu on a terminal). To force
a fresh login, `logout --env X` first. Each environment's results are
separate job items too — the environment is part of the job item id, so an
EU parse never masquerades as a production one.

`ADE_ENDPOINT` is the raw-URL escape hatch: it overrides the endpoint only,
while credentials still file under the resolved environment. `ADE_API_KEY`
overrides whatever credential is stored. Keys are stored per environment
in `~/.ade/credentials.json` (mode 0600).
Every command supports `--json` for one stable JSON object on stdout.

**Headless setup never needs a terminal.** Where there is nothing to prompt
on — CI, cron, an agent harness, a wrapped shell — set `ADE_API_KEY`, or
pipe the key in: `echo $KEY | ade auth login` stores it for the resolved
environment exactly as the prompt would. (`--api-key KEY` works too, but an
inline key is visible to `ps`.) Nothing hangs waiting on an idle stdin: with
no key available the login tries browser sign-in, and where no browser can
open it exits immediately, naming all three paths.
`ade help credentials` is the same page from the CLI.

## Parse

| command | what it does |
|---|---|
| `ade parse -d invoice.pdf` | ensure parsed; artifacts land in `~/.ade` |
| `ade parse --document-url https://…/doc.pdf` | server fetches the URL |
| `ade parse -d doc.pdf --tier standard --wait 0` | cheap lane, submit-and-return |
| `ade parse -d doc.pdf --env eu` | run in the EU region (or `export ADE_ENV=eu`) |
| `ade parse -d doc.pdf --include markdown --json` | carry the markdown in the payload |
| `JOB=$(ade parse -d doc.pdf --id-only)` | just the job item id, for piping |

Every run is a **job item** — keyed by the invocation, `verb × environment ×
source path × content × params` — one flat folder under
`~/.ade/jobs/<job-item-id>/`:
`parse.json` (raw response), `parse.md` (markdown with its doc_id trailer),
`elements.json` (flat elements projection, recomputable from `parse.json`),
`meta.json` (provenance + params + identity), `job.json` (claim ticket).
`parse` is a guarantee: re-running the exact same invocation is served from
disk with zero API calls (`--force` re-parses in place); changed params —
or a moved/edited file — mint a *sibling* job item, so variants coexist and
nothing is silently replaced; a pending run is resumed, never resubmitted;
Ctrl-C stops the waiting, not the work — the same command resumes.
Exit codes: `0` parsed, `1` failed, `2` usage, `3` still pending (re-run the
same command to resume), `4` rate-limited before submit.
`--json` carries the summary; `--include markdown` / `--include elements`
add the bulk artifacts to it, so a script never has to reach into the store
for them. `--id-only` prints just the job item id — the piping spelling:

```sh
JOB=$(ade parse -d doc.pdf --wait 0 --id-only)   # submit and return, exit 3
ade parse -d doc.pdf --id-only                    # re-run to resume; same id
```

## Extract

| command | what it does |
|---|---|
| `ade extract JOB_ID --schema schema.json` | extract a completed parse job item |
| `ade extract -d invoice.pdf --schema '…'` | by path: reuse the latest parse, else parse first |
| `ade extract --markdown notes.md --schema '…'` | bring-your-own markdown |
| `ade extract --markdown-url https://…/notes.md --schema schema.json` | bring-your-own markdown, fetched by the server |

Every extraction is its own top-level job item. Given a parse job item id,
the extract item *references* the parse (`parse/ref.json`) — artifacts are
never copied — and **inherits the parse item's environment** (its
server-side parse run exists nowhere else; a conflicting `--env` is
refused). Given a document path, the latest completed parse of that
path+content in the target environment is reused (logged, referenced like
the id form); if none
exists the CLI **runs a standalone parse first** — a normal top-level
parse item, exactly as if you had run `parse -d` — then the extract
referencing it, both bills itemised in one summary. Every parse the CLI
runs is reusable, so repeated `extract -d` on a never-parsed document
bills the parse exactly once. Bring-your-own markdown is copied in as
`markdown.md` (spans index exactly those bytes); it has no parse edge, so
evidence degrades to spans-only. The schema must define at least one
property — an empty schema is refused locally (`empty_schema`) before
anything is submitted or billed.

The extraction itself is on stdout: `--json` carries the schema-shaped
result under `extraction`, alongside the per-field `evidence` join —
reading `extract.json` out of the store is a convenience, never a
requirement.

```sh
ade extract $JOB --schema schema.json --json | jq .extraction
```

## View

| command | what it does |
|---|---|
| `ade view` | the latest *viewable* job item's HTML viewer (opens in your browser on a terminal) |
| `ade view JOB_ID` | a specific job item (id or unambiguous prefix) |
| `ade view JOB_ID --element-id ELEMENT_ID` | deep link straight to one element |
| `ade crop JOB_ID --element-id ELEMENT_ID` | PNG of just that element's region |
| `ade crop JOB_ID --type figure` | PNG of every figure — one command, no loop |
| `ade crop JOB_ID --all -o ./crops` | every element, into a directory you pick |

On a terminal, `view` opens the viewer in your browser by default
(`--no-open` suppresses it); `--json` runs and piped output never
launch a browser — the artifact path is in the output either way.
Without a JOB_ID, `view` targets the latest viewable job item.

Every job item gets a **self-contained `view.html`**: page images with
bounding-box overlays beside the parsed markdown (or extraction JSON),
selection synced across both panes, and a history sidebar for jumping
between items. Deep links are the citation contract — `--element-id`
emits `view.html#element=…`, which opens with that element selected on
both sides. `crop` cuts element regions straight out of the source
document (`--dpi`, default 300) — visual evidence for an answer, one
command. Address one element with `--element-id` (ids from `ade find`),
or crop in batch with the very same filters `find` searches by:
`--type figure`, `--page 3`, `--all`. Crops land in the job item's
`crops/` (or a directory you name with `-o`; a single crop's `-o` may
also name the PNG path itself), and `--json` always returns the same
shape — `count` plus one `crops[]` record per PNG — whether one element
matched or many. Everything renders from the
local store: zero API calls, rebuilds fingerprint-gated. Long documents
stay browsable end to end — the artifact embeds the first 40 pages and
loads the rest on demand from sidecar files rendered into the store.

## History

| command | what it does |
|---|---|
| `ade history` | defaults to `ade history list` |
| `ade history list` | one row per job item: id, kind, state, params, source |
| `ade history list --json` | full records (params verbatim, timestamps, linkage) |
| `ade history clear JOB_ID` | delete an item; clearing a parse cascades to its extracts |
| `ade history clear --all` | delete every stored job item |

The read model over the store — zero API calls; states derive from tickets
and artifacts on disk. Extract items referencing a parse render as indented
child rows beneath it; an extraction whose parse was re-run in place with
`--force` is marked **stale** (listing annotation, `"stale": true` in
`--json`, and an amber mark with a hover tooltip in the viewer sidebar) —
re-run `ade extract` to refresh it. Every `history`/`view` run re-scans
`jobs/` and rewrites `~/.ade/history.js` (the viewer sidebar's read model),
so manually deleted folders simply vanish from listings.

Commands that read the store (`view`, `crop`, `find`, `history clear`,
`extract JOB_ID`) take a **job item id or an unambiguous prefix** — paths
are accepted only by the convenience verbs (`parse -d`, `extract -d`); run
`ade history list` when you have a path and need an id.

## Find

| command | what it does |
|---|---|
| `ade find JOB_ID "total"` | case-insensitive substring |
| `ade find --job ITEM_A --job ITEM_B --json "€42"` | multi-item (`--job`, repeatable), tagged by `job_item_id` |
| `ade find JOB_ID --type table_cell --regex '€\d+'` | filters compose: type + regex |
| `ade find JOB_ID --page 1 --limit 5` | no query = every element |

Pure local search over a parse job item's elements projection — zero API
calls, no ranking. All filters compose (AND): QUERY (substring, or a regex
with `--regex`), `--type`, `--page`, `--element-id` (repeatable), `--limit`.
Matches are the citation currency, one record per element in document order
(page, then reading order): `{job_item_id, element_id, type, page, box, text}`.
`--id-only` prints the element ids alone, one per line, for piping into
anything that takes them — though `crop` accepts these same filters
directly, so cropping a selection needs no bridge.

## Help topics

`ade help` is the whole-surface reference in one call. It also carries the
conceptual pages a per-command `--help` has nowhere to put:

| topic | what it explains |
|---|---|
| `ade help workflow` | how the verbs compose — the pipeline, before your first run |
| `ade help output` | `--json`, `--id-only`, and where results live |
| `ade help credentials` | environment resolution and headless setup |
| `ade help errors` | exit states and the error payload shape |

`ade help COMMAND` scopes the reference to one command — including the
published shape of its `--json` result.

## Agents

Agents are first-class callers: every command takes `--json` and emits one
stable JSON object whose shape is published per verb (`ade help --json`
carries a `result` block for each), the full result is always on stdout —
never only in a store file — `ade help --json` returns the entire shipped
surface (commands, flags, result shapes, topics, exit states, store
layout) in a single call, and [`SKILL.md`](SKILL.md) ships the agent
contract: the loop, the conventions, and the sharp edges.

Automating installs: the binary lands at `~/.ade/bin/ade`, and the
installer ends by naming that absolute path. Non-interactive shells (CI,
cron, agent harnesses) source no rc file, so a PATH entry your login shell
has may not be there — call the absolute path, or export
`PATH="$HOME/.ade/bin:$PATH"` in the job itself.

## History

This repository was **`agentic-doc`**, the original Agentic Document
Extraction SDK. The SDK is preserved unchanged — and no longer
developed — on the
[`legacy`](https://github.com/landing-ai/ade-cli/tree/legacy) branch;
since 2026-07-31 this repo ships the ADE CLI.

## License

[Apache-2.0](LICENSE).
