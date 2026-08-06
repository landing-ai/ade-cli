# Developing ade-cli

The user-facing story lives in [README.md](README.md); this file is the
developer's map. The command is `ade`; the package keeps the `ade-cli`
name.

## Setup

```sh
uv sync
uv run ade --help
uv run pytest -q
```

Tests are fully offline (fake transport). Lint config lives in
`pyproject.toml` (`ruff`).

The one exception is `tests/integration/` — a live suite that drives
the CLI as real subprocesses against production (auth, parse, extract,
find, crop). It skips itself entirely unless `ADE_INTEGRATION_API_KEY`
holds a production API key, so it never runs by accident; when it does
run it bills real credits (one parse + one extract). Run it on demand:

```sh
ADE_INTEGRATION_API_KEY=<key> uv run pytest tests/integration -v
```

In CI it runs via `.github/workflows/integration.yml` on macOS and
Windows — on every push to `main` (a merged PR that breaks production
integration surfaces immediately), manually from **Actions →
Integration → "Run workflow"**, and as the release gate (below). Never
on pull requests: PR CI (`ci.yml`) runs the offline suite only.

## Install from source (no clone)

```sh
uv tool install git+https://github.com/landing-ai/ade-cli.git
```

(`pipx install git+https://…` works too, and
`git+ssh://git@github.com/…` if you prefer SSH. Upgrade later with
`uv tool upgrade ade-cli`; pin a ref with `@main` / `@v0.2.0` on the
URL.) End users should prefer the standalone binaries in the README —
no Python required.

## Where the design lives

- `docs/ade-cli-v2-proposal.md` — the design document the CLI is built
  against.
- `CONTEXT.md` — the domain glossary. Use its vocabulary in issues,
  PRs, and code; don't drift to the synonyms it explicitly avoids.
- `docs/adr/` — decisions with their whys (e.g. ADR-0003 per-invocation
  environments, ADR-0004/ADR-0008 browser OAuth dark at launch, then
  public).
- `SKILL.md` + `docs/reference/help.json` — the agent contract. `help`
  is generated from the live command tree; regenerate the committed
  snapshot after any surface change:

  ```sh
  uv run python scripts/update_help_reference.py
  ```

- `docs/telemetry.md`, `docs/user-agent.md` — wire-visible contracts.

## Releasing

`pyproject.toml` is the single source of truth for the version; a
release is the tag `v<version>`:

1. Bump `version` in `pyproject.toml` **and** `uv.lock` (run `uv lock`);
   land both on `main` through a PR.
2. Either **Actions → Release → "Run workflow"** on `main` (the workflow
   tags `v<version>` itself, refusing an already-released version), or
   locally: `git tag v0.3.0 && git push origin v0.3.0`.

The `Release` workflow (`.github/workflows/release.yml`) first runs the
live integration suite against production on macOS and Windows
(`integration.yml`, secret `ADE_INTEGRATION_API_KEY`) — a dispatch release
isn't even tagged until it passes. It then refuses a tag
that doesn't match `pyproject.toml`, re-runs the test suite, builds a
standalone PyInstaller app for each of the six platforms (macOS /
Linux / Windows × arm64 / x86_64), runs `ade version` on every one, and
publishes the archives, `SHA256SUMS.txt`, and the install scripts as a
GitHub release. `tests/test_release_pipeline.py` keeps the workflow
matrix, the install scripts, and the version policy from drifting
apart.

## Note: pre-1.0 internal stores

Pre-job-item versions of this CLI kept `~/.ade/docs/<doc-id>/` trees.
Those are not migrated and are invisible to `ade history list`; reclaim
the space with `rm -rf ~/.ade/docs`.
