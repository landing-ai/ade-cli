"""``update`` — self-update against the GitHub release channel (#138),
driven through the CLI seam.

The release channel is the fake transport: the version probe, the asset
download, and SHA256SUMS.txt are all scripted. The frozen-binary mode is
entered through the same seam the viewer re-exec uses (``sys.frozen`` +
``sys.executable``), pointed at a fake install dir under tmp_path — the
swap is asserted on real files.
"""

import hashlib
import io
import json
import sys
import tarfile
import zipfile
from importlib.metadata import version as installed_version

import httpx
import pytest

from ade_cli import update as update_mod

KEY = "sk-test-0123456789abcd"


def script_latest(cli, tag="v99.0.0", status=200):
    cli.transport.respond(status, {"tag_name": tag} if status == 200 else {})


# --- version check, python-mode install ------------------------------------


def test_update_reports_newer_version_and_points_at_uv_for_python_installs(cli):
    script_latest(cli, "v99.0.0")

    result = cli.invoke("update", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "current": installed_version("ade-cli"),
        "latest": "99.0.0",
        "updated": False,
        "install": "python",
    }
    (request,) = cli.transport.requests
    assert request.url == update_mod.RELEASES_LATEST_URL

    script_latest(cli, "v99.0.0")
    human = cli.invoke("update")
    assert human.exit_code == 0
    # A Python environment ade does not manage is never mutated.
    assert "uv tool upgrade ade-cli" in human.stdout


def test_update_up_to_date_reports_and_changes_nothing(cli):
    script_latest(cli, f"v{installed_version('ade-cli')}")

    result = cli.invoke("update", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["updated"] is False
    assert payload["latest"] == installed_version("ade-cli")


def test_an_ambient_github_token_rides_on_the_version_check(cli):
    # install.sh's posture: a GITHUB_TOKEN/GH_TOKEN in the environment is
    # used, never stored — it lifts the anonymous API rate limit and
    # covers the private-repo window.
    script_latest(cli, "v99.0.0")

    result = cli.invoke(
        "update", "--json", env={"GITHUB_TOKEN": "ghp_test_token"}
    )

    assert result.exit_code == 0
    (request,) = cli.transport.requests
    assert request.headers["Authorization"] == "Bearer ghp_test_token"

    script_latest(cli, "v99.0.0")
    bare = cli.invoke("update", "--json", env={"GITHUB_TOKEN": None, "GH_TOKEN": None})
    assert bare.exit_code == 0
    assert "Authorization" not in cli.transport.requests[-1].headers


def test_update_skips_silently_when_the_release_channel_is_not_visible(cli):
    # 404 is what the unauthenticated API answers while the repo is
    # private (or has no release yet): nothing to compare, not a failure.
    cli.transport.respond(404, {"message": "Not Found"})

    result = cli.invoke("update", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["latest"] is None


def test_update_check_failure_is_a_structured_error(cli):
    cli.transport.respond(500, {"message": "boom"})

    result = cli.invoke("update", "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "update_check_failed"


# --- frozen-binary self-replace ---------------------------------------------


@pytest.fixture
def frozen_install(tmp_path, monkeypatch):
    """A fake onedir install this process 'runs from': sys.frozen plus
    sys.executable pointed inside it — the same seam the viewer re-exec
    reads."""
    root = tmp_path / "install"
    (root / "_internal").mkdir(parents=True)
    (root / "_internal" / "old-lib.bin").write_bytes(b"old lib")
    binary = root / "ade"
    binary.write_bytes(b"old binary")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(binary))
    return root


def release_archive_bytes(tmp_path, asset, binary=b"new binary"):
    """A release asset with the app layout the installers unpack:
    ade/<binary> + ade/_internal/."""
    app = tmp_path / "bundle" / "ade"
    (app / "_internal").mkdir(parents=True, exist_ok=True)
    (app / "_internal" / "new-lib.bin").write_bytes(b"new lib")
    (app / "ade").write_bytes(binary)
    buffer = io.BytesIO()
    if asset.endswith(".zip"):
        with zipfile.ZipFile(buffer, "w") as bundle:
            for path in sorted(app.rglob("*")):
                bundle.write(path, "ade/" + str(path.relative_to(app)))
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            bundle.add(app, arcname="ade")
    return buffer.getvalue()


def script_release(cli, tmp_path, *, corrupt_sums=False):
    """Script the whole self-update conversation: the version probe, the
    platform asset, and SHA256SUMS.txt."""
    asset = update_mod.asset_name(update_mod.platform_target())
    payload = release_archive_bytes(tmp_path, asset)
    digest = hashlib.sha256(payload).hexdigest()
    if corrupt_sums:
        digest = "0" * 64
    script_latest(cli, "v99.0.0")
    cli.transport.respond_with(lambda request: httpx.Response(200, content=payload))
    cli.transport.respond_with(
        lambda request: httpx.Response(
            200, content=f"{digest}  {asset}\n".encode()
        )
    )
    return asset


def test_frozen_update_verifies_downloads_and_swaps_atomically(
    cli, tmp_path, frozen_install
):
    asset = script_release(cli, tmp_path)

    result = cli.invoke("update", "--yes", "--json")

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["updated"] is True
    assert payload["install"] == "binary"
    assert payload["latest"] == "99.0.0"
    # The swap really happened, and the whole app moved together.
    assert (frozen_install / "ade").read_bytes() == b"new binary"
    assert (frozen_install / "_internal" / "new-lib.bin").exists()
    assert not (frozen_install / "_internal" / "old-lib.bin").exists()
    # Staging and aside-renamed leftovers are gone (POSIX sweeps in-run).
    assert not list(frozen_install.glob(".ade-update-*"))
    assert not list(frozen_install.glob("*.old.*"))
    # The conversation hit the versionless latest/download URLs.
    urls = [str(request.url) for request in cli.transport.requests]
    assert urls[1] == f"{update_mod.DOWNLOAD_BASE_URL}/{asset}"
    assert urls[2] == f"{update_mod.DOWNLOAD_BASE_URL}/SHA256SUMS.txt"


def test_frozen_update_reports_progress_on_stderr(cli, tmp_path, frozen_install):
    # The download runs tens of seconds against the real channel and read
    # as a hang without feedback. Milestones ride the guarantee's
    # progress line: stderr only (stdout keeps the summary), silent
    # under --json.
    asset = script_release(cli, tmp_path)

    result = cli.invoke("update", "--yes")

    assert result.exit_code == 0, result.stdout
    assert f"downloading {asset}" in result.stderr
    assert "verifying checksum" in result.stderr
    assert "installing" in result.stderr
    assert "updated to 99.0.0" in result.stderr
    assert "downloading" not in result.stdout

    # --json stays fully silent on both streams.
    (frozen_install / "ade").write_bytes(b"old binary")
    script_release(cli, tmp_path)
    silent = cli.invoke("update", "--yes", "--json")
    assert silent.exit_code == 0
    assert silent.stderr == ""


def test_frozen_update_refuses_a_checksum_mismatch(cli, tmp_path, frozen_install):
    script_release(cli, tmp_path, corrupt_sums=True)

    result = cli.invoke("update", "--yes", "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "checksum_mismatch"
    # Nothing swapped, nothing staged left behind.
    assert (frozen_install / "ade").read_bytes() == b"old binary"
    assert (frozen_install / "_internal" / "old-lib.bin").exists()
    assert not list(frozen_install.glob(".ade-update-*"))


def test_frozen_update_without_a_terminal_requires_yes(
    cli, tmp_path, frozen_install
):
    script_latest(cli, "v99.0.0")

    result = cli.invoke("update", "--json")  # stdin is not a tty, no --yes

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "confirm_required"
    assert (frozen_install / "ade").read_bytes() == b"old binary"


def test_frozen_update_confirmation_declined_changes_nothing(
    cli, tmp_path, frozen_install
):
    cli.stdin_tty = True
    script_latest(cli, "v99.0.0")

    result = cli.invoke("update", input="n\n")

    assert result.exit_code == 0
    assert "nothing changed" in result.stdout
    assert (frozen_install / "ade").read_bytes() == b"old binary"
    assert len(cli.transport.requests) == 1  # probe only, no download


def test_a_later_run_sweeps_windows_style_leftovers(cli, frozen_install):
    # Windows cannot delete the running .exe or its mapped DLLs during
    # the swap; they are renamed aside and swept by a later run — any
    # command's post-run hook, exercised here through the seam.
    (frozen_install / "ade.old.123").write_bytes(b"stale")
    (frozen_install / "_internal.old.123").mkdir()

    result = cli.invoke("history", "list", "--json")

    assert result.exit_code == 0
    assert not (frozen_install / "ade.old.123").exists()
    assert not (frozen_install / "_internal.old.123").exists()


# --- the periodic check + nudge ---------------------------------------------

NUDGE_ENV = {"ADE_NO_UPDATE_CHECK": None}  # opt back in past the harness shield


def test_a_command_nudges_on_stderr_once_a_day_when_newer_exists(cli):
    cli.stderr_tty = True
    script_latest(cli, "v99.0.0")

    first = cli.invoke("history", "list", "--json", env=NUDGE_ENV)

    assert first.exit_code == 0
    # stdout keeps its one-stable-JSON-object contract; the nudge is
    # stderr-only.
    assert json.loads(first.stdout) == []
    assert "ade 99.0.0 is available" in first.stderr
    assert "ade update" in first.stderr
    assert len(cli.transport.requests) == 1
    assert (cli.home / update_mod.CACHE_NAME).exists()

    # Within the throttle window: no probe, no repeat nudge.
    second = cli.invoke("history", "list", "--json", env=NUDGE_ENV)
    assert second.exit_code == 0
    assert "ade update" not in second.stderr
    assert len(cli.transport.requests) == 1


def test_the_nudge_is_suppressed_without_a_tty(cli):
    result = cli.invoke("history", "list", "--json", env=NUDGE_ENV)

    assert result.exit_code == 0
    assert cli.transport.requests == []  # never even probed


def test_the_nudge_opt_out_is_respected(cli):
    cli.stderr_tty = True

    result = cli.invoke("history", "list", "--json")  # shield stays on

    assert result.exit_code == 0
    assert cli.transport.requests == []


def test_a_failed_check_never_surfaces_and_stamps_the_throttle(cli):
    cli.stderr_tty = True
    cli.transport.respond(500, {"message": "boom"})

    first = cli.invoke("history", "list", "--json", env=NUDGE_ENV)

    assert first.exit_code == 0
    assert "update" not in first.stderr
    # The attempt is stamped: an unreachable channel is one probe per
    # day, not one per command.
    second = cli.invoke("history", "list", "--json", env=NUDGE_ENV)
    assert second.exit_code == 0
    assert len(cli.transport.requests) == 1


def test_running_update_itself_buys_a_quiet_day(cli):
    script_latest(cli, "v99.0.0")
    checked = cli.invoke("update", "--json")
    assert checked.exit_code == 0

    cli.stderr_tty = True
    result = cli.invoke("history", "list", "--json", env=NUDGE_ENV)

    assert result.exit_code == 0
    assert len(cli.transport.requests) == 1  # update's own probe, nothing since


# --- the unknown-model hint ---------------------------------------------------


def test_an_unknown_model_rejection_hints_at_update(cli, tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 bytes")
    cli.transport.respond(
        422, {"code": "validation_error", "message": "Unknown model 'dpt-4-pro'"}
    )

    result = cli.invoke(
        "parse", "-d", str(doc), "--model", "dpt-4-pro", "--json",
        env={"ADE_API_KEY": KEY},
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "http"
    assert "ade update" in payload["hint"]

    cli.transport.respond(
        422, {"code": "validation_error", "message": "Unknown model 'dpt-4-pro'"}
    )
    human = cli.invoke(
        "parse", "-d", str(doc), "--model", "dpt-4-pro", env={"ADE_API_KEY": KEY}
    )
    assert human.exit_code == 1
    assert "ade update" in human.stdout


def test_unrelated_http_errors_carry_no_update_hint(cli, tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 bytes")
    cli.transport.respond(
        422, {"code": "validation_error", "message": "options.pages out of range"}
    )

    result = cli.invoke("parse", "-d", str(doc), "--json", env={"ADE_API_KEY": KEY})

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "http"
    assert "hint" not in payload
