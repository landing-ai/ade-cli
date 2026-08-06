"""Release pipeline contract: the tag-triggered workflow, the install
scripts, and pyproject must agree on the six shipped targets, the asset
naming scheme, and the version source of truth.

The pipeline itself only runs on a pushed tag, so these are string-level
checks over the checked-in files — enough to stop the pieces drifting
apart silently (a target dropped from the matrix, an asset rename that
strands the installers, a version bump the tag check would reject).
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
INTEGRATION = ROOT / ".github" / "workflows" / "integration.yml"
INSTALL_SH = ROOT / "scripts" / "install.sh"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"
INSTALL_CMD = ROOT / "scripts" / "install.cmd"

# Asset names are versionless (`ade-cli-<target>.<ext>`) so the installers
# can address `releases/latest/download/...` without parsing tags.
UNIX_TARGETS = {"darwin-arm64", "darwin-x86_64", "linux-arm64", "linux-x86_64"}
WINDOWS_TARGETS = {"windows-arm64", "windows-x86_64"}


def test_release_workflow_matrix_covers_all_six_targets():
    text = WORKFLOW.read_text(encoding="utf-8")
    targets = set(re.findall(r"target:\s*(\S+)", text))
    assert targets == UNIX_TARGETS | WINDOWS_TARGETS


def test_release_workflow_triggers_on_version_tags_and_checks_pyproject():
    """Version policy: pyproject.toml is the source of truth; a release is
    the tag v<version>, and the workflow refuses a mismatched tag."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert '"v*"' in text
    assert "GITHUB_REF_NAME" in text
    assert "pyproject.toml" in text


def test_release_workflow_is_manually_dispatchable():
    """Cutting a release must not require a local clone: manual dispatch
    tags v<pyproject version> itself (a GITHUB_TOKEN-pushed tag never
    triggers workflows, so the tagging has to live inside this one)."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text
    assert "git tag" in text


def test_release_gates_on_the_live_integration_suite():
    """Issue #175: nothing ships without the production integration suite
    passing. The gate must sit ahead of `check` — under manual dispatch
    `check` pushes the release tag, so a failed gate must leave no tag —
    and everything downstream inherits it through `needs`."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/integration.yml" in text
    check_job = text.split("\n  check:", 1)[1]
    assert re.search(r"^\s*needs: integration\b", check_job, re.MULTILINE)


def test_integration_suite_runs_on_macos_and_windows():
    """The two platforms customers install on (issue #175). Manual
    dispatch stays available, and push/PR must never trigger it — every
    run bills real parse + extract credits against production."""
    text = INTEGRATION.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text
    assert "workflow_call" in text
    assert re.search(r"runner:\s*macos-", text)
    assert re.search(r"runner:\s*windows-", text)
    assert "push:" not in text
    assert "pull_request:" not in text
    assert "ADE_INTEGRATION_API_KEY" in text
    assert "tests/integration" in text


def test_integration_tests_skip_without_the_live_key():
    """The offline suite must stay hermetic: every test in
    tests/integration/ hangs off the ADE_INTEGRATION_API_KEY gate."""
    for path in sorted((ROOT / "tests" / "integration").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        assert "ADE_INTEGRATION_API_KEY" in text, f"{path.name} misses the gate"
        assert re.search(r"^pytestmark = pytest\.mark\.skipif", text, re.MULTILINE), (
            f"{path.name} does not skip module-wide without the key"
        )


def test_release_builds_onedir_not_onefile():
    """--onefile re-extracts ~100 shared libraries to fresh inodes on every
    launch, so macOS re-validates every code signature every run — ~10s per
    command (issue #83). The build must stay onedir, and both installers must
    carry the app's _internal support dir alongside the binary."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--onedir" in text
    assert "--onefile" not in text
    assert "_internal" in INSTALL_SH.read_text(encoding="utf-8")
    assert "_internal" in INSTALL_PS1.read_text(encoding="utf-8")


def test_release_workflow_smoke_tests_the_frozen_binary():
    """Every built binary must run `version` before it ships — the one
    command that breaks first when PyInstaller drops the dist metadata."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"[/\\]ade(\.exe)?(\"| )+version", text)


def test_install_sh_maps_every_unix_target():
    text = INSTALL_SH.read_text(encoding="utf-8")
    for target in sorted(UNIX_TARGETS):
        assert target in text, f"install.sh does not handle {target}"
    assert ".tar.gz" in text
    assert os.access(INSTALL_SH, os.X_OK), "install.sh must be executable"


def test_install_ps1_maps_every_windows_target():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    for target in sorted(WINDOWS_TARGETS):
        assert target in text, f"install.ps1 does not handle {target}"
    assert ".zip" in text


def test_install_sh_links_into_local_bin():
    """The no-PATH-edit story: the installer symlinks the binary into
    ~/.local/bin (XDG user bin, already on PATH in most setups) —
    creating the dir when it is missing — instead of asking the user to
    edit rc files, and proves the link runs before reporting it."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert ".local/bin" in text
    assert "ln -s" in text
    assert 'mkdir -p "$local_bin"' in text
    assert '"$link" version' in text


def test_installers_end_with_the_absolute_path_for_non_interactive_shells():
    """F8: the PATH check reads the *installing* shell's PATH, but CI jobs,
    cron, and agent harnesses spawn non-interactive shells that source no
    rc file. The install must therefore always end by naming the absolute
    path — and, while it is talking to machine callers, the `help --json`
    bootstrap (F9)."""
    for script, binary in ((INSTALL_SH, "/ade"), (INSTALL_PS1, "\\ade.exe")):
        text = script.read_text(encoding="utf-8")
        assert f"{binary} help --json" in text, f"{script.name} hides the bootstrap"
        assert "--json" in text.rsplit("help --json", 1)[1], (
            f"{script.name} never tells machine callers to pass --json"
        )


def test_install_cmd_delegates_to_the_powershell_installer():
    """CMD is a thin wrapper: one installer body (install.ps1) serves both
    Windows shells, so arch mapping is asserted only there."""
    text = INSTALL_CMD.read_text(encoding="utf-8")
    assert "install.ps1" in text
    assert "powershell" in text.lower()


def test_installers_download_from_this_repo():
    repo = "landing-ai/ade-cli"
    assert repo in INSTALL_SH.read_text(encoding="utf-8")
    assert repo in INSTALL_PS1.read_text(encoding="utf-8")
    assert repo in INSTALL_CMD.read_text(encoding="utf-8")


def test_posix_only_imports_are_quarantined_in_filelock():
    """The frozen binary must start on Windows (v0.1.0's smoke test caught
    a top-level `import fcntl` crashing it). Platform-specific locking
    lives in filelock.py behind an os.name guard; everywhere else imports
    must be portable."""
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.name == "filelock.py":
            continue
        text = path.read_text(encoding="utf-8")
        for module in ("fcntl", "msvcrt", "termios", "pwd", "grp", "resource"):
            assert not re.search(
                rf"^\s*(?:import|from)\s+{module}\b", text, re.MULTILINE
            ), f"{path.relative_to(ROOT)} imports POSIX/Windows-only {module}"


def test_pyproject_version_is_a_plain_semver():
    """The tag check and asset URLs assume `v<major>.<minor>.<patch>`."""
    with (ROOT / "pyproject.toml").open("rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
