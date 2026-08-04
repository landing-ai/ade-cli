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
- `docs/agents/writing-style.md` — standards for help text, the README,
  and SKILL.md. Backticked inline code in help prose is enforced by
  `tests/test_help_style.py`; the hosted docs generate their CLI
  reference from `help --json`, so these strings are customer-facing.

## Releasing

`pyproject.toml` is the single source of truth for the version; a
release is the tag `v<version>`:

1. Bump `version` in `pyproject.toml` **and** `uv.lock` (run `uv lock`);
   land both on `main` through a PR.
2. Either **Actions → Release → "Run workflow"** on `main` (the workflow
   tags `v<version>` itself, refusing an already-released version), or
   locally: `git tag v0.3.0 && git push origin v0.3.0`.

The `Release` workflow (`.github/workflows/release.yml`) refuses a tag
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
