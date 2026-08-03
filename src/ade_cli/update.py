"""``update`` — keep the CLI current against a moving backend (#138).

Two install modes, detected at runtime:

- **Frozen binary** (``sys.frozen`` — the same seam the viewer re-exec
  uses): download this platform's asset from the GitHub release channel
  (versionless names, so ``releases/latest/download/...`` resolves
  without knowing the tag), verify it against ``SHA256SUMS.txt``, and
  replace the running app by staging inside the install dir and
  renaming — mirroring install.sh. Windows cannot overwrite a running
  ``.exe``: the old pieces are renamed aside (``*.old.<pid>``) and swept
  by a later run.
- **Python environment** (uv/pipx/pip — not frozen): an environment this
  CLI does not manage is never mutated; ``update`` reports the newer
  version and points at ``uv tool upgrade ade-cli``.

The periodic check rides after every command's real work (the same
post-command seam as the telemetry flush): throttled to at most one
network probe per ~24h via a cache file under the store home, and the
nudge goes to **stderr only** — stdout keeps its one-stable-JSON-object
contract. Suppressed when stderr is not a TTY (scripts and agents never
see it) and by the ``ADE_NO_UPDATE_CHECK`` variable or the
``update_check: false`` config key. Every failure is swallowed: staying
current is never worth failing a command. While the repo is private the
unauthenticated check answers 404/403 — that reads as "skip silently",
so the public transition needs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import time
import zipfile
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path
from typing import Mapping

import httpx
import typer

from .config import load_config
from .output import EXIT_FAILED, EXIT_USAGE, JSON_FLAG, emit, exit_with
from .ports import Ports

REPO = "landing-ai/ade-cli"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
# Versionless asset names (internal #76) — latest/download resolves
# without knowing the tag, which is what makes self-update one GET.
DOWNLOAD_BASE_URL = f"https://github.com/{REPO}/releases/latest/download"

# The explicit command can afford a real wait; the piggybacked check
# rides on someone else's invocation and must stay cheap even against a
# black-holed network (same posture as the telemetry flush).
CHECK_TIMEOUT_SECONDS = 10.0
NUDGE_TIMEOUT_SECONDS = 3.0
CHECK_INTERVAL_SECONDS = 24 * 3600.0
CACHE_NAME = "update-check.json"


class UpdateCheckError(Exception):
    """The release channel could not answer the version question."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_mode() -> str:
    """How this CLI is installed: ``binary`` (the frozen standalone app —
    self-update replaces it in place) or ``python`` (uv/pipx/pip — an
    environment ade does not manage and never mutates)."""
    return "binary" if is_frozen() else "python"


def install_root() -> Path:
    """The onedir app root: the directory holding the running binary and
    its ``_internal/`` support dir. Meaningful only when frozen."""
    return Path(sys.executable).resolve().parent


def current_version() -> str:
    try:
        return _installed_version("ade-cli")
    except PackageNotFoundError:
        return "unknown"


def _parse_version(text: str) -> tuple[int, ...] | None:
    parts = text.strip().lstrip("v").split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def is_newer(candidate: str | None, current: str) -> bool:
    """Whether ``candidate`` is a strictly newer release than ``current``.
    Unparseable versions are never "newer" — a nudge must not fire on
    garbage, and an unknown local version has nothing to compare."""
    if candidate is None:
        return False
    new, old = _parse_version(candidate), _parse_version(current)
    if new is None or old is None:
        return False
    return new > old


def fetch_latest(
    transport: httpx.BaseTransport, *, timeout: float
) -> str | None:
    """The latest release's version (tag without the ``v``), or None when
    the channel is unavailable in the way that means "skip silently":
    404/403 is what the unauthenticated API answers while the repo is
    private (or has no release yet). Anything else — network failure, an
    unexpected status, a malformed body — raises UpdateCheckError."""
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.get(
                RELEASES_LATEST_URL,
                headers={"Accept": "application/vnd.github+json"},
                follow_redirects=True,
            )
    except httpx.HTTPError as error:
        raise UpdateCheckError(f"cannot reach the release channel: {error}")
    if response.status_code in (403, 404):
        return None
    if response.status_code != 200:
        raise UpdateCheckError(
            f"release channel answered HTTP {response.status_code}"
        )
    try:
        tag = response.json().get("tag_name")
    except json.JSONDecodeError:
        tag = None
    if not isinstance(tag, str) or not tag:
        raise UpdateCheckError("release channel sent no tag_name")
    return tag.lstrip("v")


# ---------------------------------------------------------------------------
# The `ade update` command


def platform_target() -> str | None:
    """This machine's release-asset target (``darwin-arm64``, ...), the
    exact vocabulary the release workflow and installers share; None for
    a platform the release matrix does not build."""
    machines = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }
    arch = machines.get(platform.machine().lower())
    if arch is None:
        return None
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    if sys.platform.startswith("linux"):
        return f"linux-{arch}"
    if sys.platform in ("win32", "cygwin"):
        return f"windows-{arch}"
    return None


def asset_name(target: str) -> str:
    ext = "zip" if target.startswith("windows-") else "tar.gz"
    return f"ade-cli-{target}.{ext}"


def update(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Install without the interactive confirmation (required when "
        "stdin is not a terminal).",
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Check the release channel for a newer CLI and self-update on
    confirmation. A standalone-binary install (see `ade version`)
    replaces itself in place after verifying the release checksum; a
    uv/pipx install is never mutated — the command reports the newer
    version and points at `uv tool upgrade ade-cli`."""
    ports: Ports = ctx.obj
    current = current_version()
    mode = install_mode()
    try:
        latest = fetch_latest(ports.transport, timeout=CHECK_TIMEOUT_SECONDS)
    except UpdateCheckError as error:
        exit_with(
            {"error": "update_check_failed", "message": str(error), "current": current},
            f"Cannot check for updates: {error}.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    # The explicit check counts against the periodic throttle too — a
    # fresh `update` should buy a quiet day, whatever it concluded.
    _write_cache(_home(os.environ), latest=latest)
    payload = {"current": current, "latest": latest, "updated": False, "install": mode}
    if latest is None:
        emit(
            payload,
            "The release channel has no visible release to compare against; "
            "nothing to do.",
            as_json=as_json,
        )
        return
    if not is_newer(latest, current):
        emit(payload, f"ade {current} is up to date (latest: {latest}).", as_json=as_json)
        return
    if mode == "python":
        emit(
            payload,
            f"ade {latest} is available (you have {current}). This is a "
            "Python-environment install ade does not manage — run "
            "`uv tool upgrade ade-cli` (or your installer's equivalent) to "
            "update.",
            as_json=as_json,
        )
        return
    if not yes:
        if not ports.stdin_is_tty():
            exit_with(
                {
                    "error": "confirm_required",
                    "current": current,
                    "latest": latest,
                    "message": "pass --yes to update without a terminal to confirm on",
                },
                f"ade {latest} is available (you have {current}); updating "
                "replaces the installed binary. Pass --yes to confirm "
                "non-interactively.",
                as_json=as_json,
                code=EXIT_USAGE,
            )
        if not typer.confirm(f"Update ade {current} -> {latest}?"):
            emit(payload, "Update declined; nothing changed.", as_json=as_json)
            return
    _self_replace(ports.transport, latest=latest, as_json=as_json)
    emit(
        {**payload, "updated": True},
        f"Updated ade {current} -> {latest} in {install_root()} — takes "
        "effect on the next run.",
        as_json=as_json,
    )


def _self_replace(
    transport: httpx.BaseTransport, *, latest: str, as_json: bool
) -> None:
    """Download, verify, and swap in the latest frozen app. Mirrors
    install.sh: stage *inside* the install dir so the final steps are
    same-filesystem renames, verify against SHA256SUMS.txt before
    touching anything, and validate the archive's layout before the
    first rename — past that point every step is a rename that cannot
    half-copy."""
    target = platform_target()
    if target is None:
        exit_with(
            {
                "error": "unsupported_platform",
                "platform": f"{sys.platform}-{platform.machine()}",
            },
            f"No release asset exists for this platform "
            f"({sys.platform} {platform.machine()}); re-install manually.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    asset = asset_name(target)
    root = install_root()
    staging = root / f".ade-update-{os.getpid()}"
    try:
        try:
            staging.mkdir(parents=True)
        except OSError as error:
            exit_with(
                {"error": "update_failed", "message": f"cannot stage in {root}: {error}"},
                f"Cannot write to the install directory {root} ({error}); "
                "re-run with the permissions the install has, or re-install.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        archive = staging / asset
        with httpx.Client(
            transport=transport, timeout=CHECK_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            _download(client, f"{DOWNLOAD_BASE_URL}/{asset}", archive, as_json=as_json)
            sums = staging / "SHA256SUMS.txt"
            _download(
                client, f"{DOWNLOAD_BASE_URL}/SHA256SUMS.txt", sums, as_json=as_json
            )
        expected = _expected_sum(sums.read_text(encoding="utf-8"), asset)
        if expected is None:
            exit_with(
                {"error": "checksum_unavailable", "asset": asset},
                f"SHA256SUMS.txt on the release has no entry for {asset}; "
                "not installing an unverifiable download.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            exit_with(
                {
                    "error": "checksum_mismatch",
                    "asset": asset,
                    "expected": expected,
                    "actual": actual,
                },
                f"Checksum mismatch for {asset} — refusing to install a "
                "download that does not match the release's SHA256SUMS.txt.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        new_dir = staging / "new"
        _extract_archive(archive, new_dir)
        # The archive holds a onedir app: ade/<binary> + ade/_internal/.
        binary_name = Path(sys.executable).name
        new_app = new_dir / "ade"
        if not (new_app / binary_name).is_file() or not (new_app / "_internal").is_dir():
            exit_with(
                {"error": "bad_release_archive", "asset": asset},
                f"The release archive {asset} does not contain the expected "
                "app layout; not installing it.",
                as_json=as_json,
                code=EXIT_FAILED,
            )
        _swap(root, new_app, binary_name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _download(
    client: httpx.Client, url: str, dest: Path, *, as_json: bool
) -> None:
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        exit_with(
            {"error": "update_download_failed", "url": url, "message": str(error)},
            f"Download failed for {url}: {error}.",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    if response.status_code != 200:
        exit_with(
            {
                "error": "update_download_failed",
                "url": url,
                "status_code": response.status_code,
            },
            f"Download failed for {url} (HTTP {response.status_code}).",
            as_json=as_json,
            code=EXIT_FAILED,
        )
    dest.write_bytes(response.content)


def _expected_sum(sums_text: str, asset: str) -> str | None:
    """The asset's digest from SHA256SUMS.txt (``<hex>  <name>`` lines,
    sha256sum's own format)."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset:
            return parts[0].lower()
    return None


def _extract_archive(archive: Path, dest: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(dest)
        return
    with tarfile.open(archive, "r:gz") as bundle:
        try:
            bundle.extractall(dest, filter="data")
        except TypeError:  # Python without the extraction-filter API
            bundle.extractall(dest)


def _swap(root: Path, new_app: Path, binary_name: str) -> None:
    """Every step is a same-filesystem rename: the running pieces move
    aside to ``*.old.<pid>`` names first — Windows cannot overwrite a
    running ``.exe`` or its mapped DLLs, but it *can* rename them — then
    the new pieces move in. The aside-renamed leftovers are swept right
    away where the OS allows and by a later run's sweep where it does
    not."""
    marker = f".old.{os.getpid()}"
    if (root / "_internal").exists():
        os.rename(root / "_internal", root / f"_internal{marker}")
    os.rename(new_app / "_internal", root / "_internal")
    if (root / binary_name).exists():
        os.rename(root / binary_name, root / f"{binary_name}{marker}")
    os.rename(new_app / binary_name, root / binary_name)
    (root / binary_name).chmod(0o755)
    sweep_stale(root)


def sweep_stale(root: Path) -> None:
    """Remove leftovers earlier updates renamed aside (and abandoned
    staging dirs). On Windows the pieces a running process still maps
    survive the attempt — a later run's sweep gets them. Never raises."""
    try:
        entries = [
            *root.glob("*.old.*"),
            *root.glob(".ade-update-*"),
        ]
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


# ---------------------------------------------------------------------------
# The post-command periodic check


def check_enabled(env: Mapping[str, str], home: Path) -> bool:
    """ADE_NO_UPDATE_CHECK (any value but empty/0, the DO_NOT_TRACK
    convention) and the ``update_check: false`` config key both disable
    the periodic check entirely."""
    if env.get("ADE_NO_UPDATE_CHECK") not in (None, "", "0"):
        return False
    try:
        if load_config(home).get("update_check") is False:
            return False
    except Exception:
        pass
    return True


def _home(env: Mapping[str, str]) -> Path:
    return Path(env["ADE_HOME"]) if env.get("ADE_HOME") else Path.home() / ".ade"


def _read_cache(home: Path) -> dict:
    try:
        record = json.loads((home / CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def _write_cache(home: Path, *, latest: str | None) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / CACHE_NAME).write_text(
            json.dumps({"checked_at": time.time(), "latest": latest}),
            encoding="utf-8",
        )
    except OSError:
        pass


def after_command(
    *,
    command: str,
    env: Mapping[str, str],
    transport: httpx.BaseTransport,
    stderr_is_tty: bool,
) -> None:
    """The post-command entry point: sweep self-update leftovers, then —
    at most once per ~24h, TTY-gated, opt-out respected — probe the
    release channel and nudge on stderr. Runs after the command's output
    and exit path are decided; never surfaces, never raises."""
    try:
        if is_frozen():
            sweep_stale(install_root())
        if command.split(" ")[0] in ("update", "version"):
            # `update` just answered the question; `version` is often the
            # probe scripts run — neither wants a nudge racing its output.
            return
        if not stderr_is_tty:
            return
        home = _home(env)
        if not check_enabled(env, home):
            return
        cache = _read_cache(home)
        checked_at = cache.get("checked_at")
        if (
            isinstance(checked_at, (int, float))
            and time.time() - float(checked_at) < CHECK_INTERVAL_SECONDS
        ):
            return
        try:
            latest = fetch_latest(transport, timeout=NUDGE_TIMEOUT_SECONDS)
        except UpdateCheckError:
            latest = None
        # Stamp the attempt whatever it answered: an unreachable channel
        # must not turn into a probe per command.
        _write_cache(home, latest=latest)
        current = current_version()
        if is_newer(latest, current):
            typer.echo(
                f"ade {latest} is available (you have {current}) — run "
                "`ade update`.",
                err=True,
            )
    except BaseException:
        pass


# ---------------------------------------------------------------------------
# The unknown-model hint

_MODEL_ERROR_MARKERS = (
    "unknown",
    "unsupported",
    "invalid",
    "not found",
    "unrecognized",
    "no such",
)


def unknown_model_hint(detail: str | None) -> str | None:
    """The registry-style "unknown model" server error usually means the
    API moved past this CLI build — worth one pointer at `ade update`.
    Matched on the server's message text (the v2 envelope pins no code
    for it), narrowly enough that unrelated failures stay unhinted."""
    text = (detail or "").lower()
    if "model" in text and any(marker in text for marker in _MODEL_ERROR_MARKERS):
        return "If the model is newer than this CLI, `ade update` may add it."
    return None
