"""Windows hardening (#160, #161, #162): three failure classes an
automated STG suite hit on Windows — the item lock held across the unlink
of its own lock file, ``os.replace`` racing another process on the shared
``history.js``, and stdout falling back to a non-UTF-8 locale codec when
no console is attached. The Windows-only errno paths cannot fire on
POSIX, so these tests pin the cross-platform *invariants* each fix rests
on (lock/unlink ordering, replace retries, stdio encoding), plus one
subprocess repro of the encoding crash itself.
"""

import json
import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

from ade_cli import store as jobstore

from parse_fixtures import completed_job

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 fake invoice bytes")
    return path


def parse_file(cli, document):
    cli.transport.respond(202, {"job_id": "job-0001"})
    cli.transport.respond(200, completed_job())
    result = cli.invoke("parse", "-d", str(document), "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


# --- #162: write_atomic retries transient replace failures -----------------


def test_write_atomic_rides_out_transient_permission_errors(tmp_path, monkeypatch):
    """On Windows, os.replace fails with ERROR_ACCESS_DENIED while another
    process momentarily holds the destination open (every invocation
    rewrites the same history.js). A brief retry absorbs the transient
    instead of crashing the command."""
    target = tmp_path / "history.js"
    real_replace = os.replace
    failures = {"left": 3}

    def flaky(src, dst, *args, **kwargs):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(13, "Access is denied", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(jobstore.os, "replace", flaky)
    sleeps: list[float] = []
    monkeypatch.setattr(jobstore.time, "sleep", sleeps.append)

    jobstore.write_atomic(target, "window.__ADE_HISTORY__ = {};\n")

    assert target.read_text(encoding="utf-8") == "window.__ADE_HISTORY__ = {};\n"
    assert failures["left"] == 0
    assert len(sleeps) == 3  # one backoff per transient failure, no spin


def test_write_atomic_still_raises_when_the_destination_never_frees(
    tmp_path, monkeypatch
):
    """The retry is patience, not forgiveness: a destination that never
    frees still surfaces the real error instead of hanging."""

    def denied(src, dst, *args, **kwargs):
        raise PermissionError(13, "Access is denied", str(dst))

    monkeypatch.setattr(jobstore.os, "replace", denied)
    monkeypatch.setattr(jobstore.time, "sleep", lambda seconds: None)

    with pytest.raises(PermissionError):
        jobstore.write_atomic(tmp_path / "history.js", "x")


# --- #160: history clear never unlinks the lock file it holds --------------


def test_history_clear_deletes_the_lock_file_only_after_releasing_the_lock(
    cli, document, monkeypatch
):
    """Holding the item lock keeps an open handle on .ticket.lock, and
    Windows refuses to unlink a file with an open handle (WinError 32).
    The invariant, asserted cross-platform: at the instant the lock is
    released, the lock file is the one thing still on disk — the husk
    removal happens strictly after."""
    item_id = parse_file(cli, document)
    at_release: dict[str, list[str] | None] = {}
    real_lock = jobstore.JobStore.lock

    @contextmanager
    def spying_lock(self, target_id):
        with real_lock(self, target_id):
            yield
        item_dir = self.item_dir(target_id)
        at_release[target_id] = (
            sorted(entry.name for entry in item_dir.iterdir())
            if item_dir.is_dir()
            else None
        )

    monkeypatch.setattr(jobstore.JobStore, "lock", spying_lock)

    result = cli.invoke("history", "clear", item_id, "--json")

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["cleared"] == [item_id]
    assert at_release[item_id] == [".ticket.lock"]
    assert not (cli.home / "jobs" / item_id).exists()


def test_history_clear_all_shares_the_same_unlink_ordering(
    cli, document, monkeypatch
):
    item_id = parse_file(cli, document)
    at_release: dict[str, list[str] | None] = {}
    real_lock = jobstore.JobStore.lock

    @contextmanager
    def spying_lock(self, target_id):
        with real_lock(self, target_id):
            yield
        item_dir = self.item_dir(target_id)
        at_release[target_id] = (
            sorted(entry.name for entry in item_dir.iterdir())
            if item_dir.is_dir()
            else None
        )

    monkeypatch.setattr(jobstore.JobStore, "lock", spying_lock)

    result = cli.invoke("history", "clear", "--all", "--json")

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["cleared"] == [item_id]
    assert at_release[item_id] == [".ticket.lock"]
    assert not (cli.home / "jobs" / item_id).exists()


# --- #161: stdio is UTF-8 whatever the console/codepage situation ----------


class _FakeStream:
    def __init__(self, encoding):
        self.encoding = encoding
        self.reconfigured: list[dict] = []

    def reconfigure(self, **kwargs):
        self.reconfigured.append(kwargs)
        self.encoding = kwargs.get("encoding", self.encoding)


def test_stdio_is_forced_to_utf8_when_the_locale_codec_is_narrower(monkeypatch):
    """A cp1252 stdout (a console-less Windows process on a non-UTF-8
    codepage) is reconfigured to UTF-8 with a replace handler, so the help
    topics' box-drawing characters can never crash the write itself."""
    from ade_cli import main as main_mod

    out, err = _FakeStream("cp1252"), _FakeStream("cp1252")
    monkeypatch.setattr(main_mod.sys, "stdout", out)
    monkeypatch.setattr(main_mod.sys, "stderr", err)

    main_mod._force_utf8_stdio()

    assert out.reconfigured == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.reconfigured == [{"encoding": "utf-8", "errors": "replace"}]


def test_stdio_already_utf8_is_left_alone(monkeypatch):
    from ade_cli import main as main_mod

    out, err = _FakeStream("utf-8"), _FakeStream("UTF-8")
    monkeypatch.setattr(main_mod.sys, "stdout", out)
    monkeypatch.setattr(main_mod.sys, "stderr", err)

    main_mod._force_utf8_stdio()

    assert out.reconfigured == []
    assert err.reconfigured == []


def test_streams_without_reconfigure_are_skipped(monkeypatch):
    """The test runner's captures (and exotic embedders) expose no
    reconfigure — startup must not touch or trip over them."""
    from ade_cli import main as main_mod

    class Bare:
        encoding = "cp1252"

    monkeypatch.setattr(main_mod.sys, "stdout", Bare())
    monkeypatch.setattr(main_mod.sys, "stderr", Bare())

    main_mod._force_utf8_stdio()  # must simply not raise


def test_help_topic_survives_a_non_utf8_stdio_in_a_subprocess(tmp_path):
    """End-to-end repro of the QA failure mode: force the interpreter to
    pick a cp1252 stdout codec (what a console-less process on a cp1252
    Windows machine gets) and print the topic carrying the box-drawing
    pipeline diagram. Before the fix the write itself raised
    UnicodeEncodeError; with it, startup re-encodes stdout as UTF-8."""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "ADE_HOME": str(tmp_path),
        "ADE_TELEMETRY": "0",
        "ADE_NO_UPDATE_CHECK": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "ade_cli", "help", "workflow"],
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    out = result.stdout.decode("utf-8")
    assert "How the verbs compose" in out
    assert "─" in out  # the diagram's box-drawing characters survived


def test_a_husk_directory_is_invisible_and_swept_by_clear_all(cli, document):
    """The in-between state _remove_item_dir can leave behind on Windows
    (a directory holding only .ticket.lock, when a waiter's handle
    outlives the retries) must be harmless: invisible to listings and
    resolution, and swept by the next clear --all."""
    item_id = parse_file(cli, document)
    husk = cli.home / "jobs" / "deadbeef00000000"
    husk.mkdir(parents=True)
    (husk / ".ticket.lock").write_bytes(b"")

    listed = cli.invoke("history", "list", "--json")
    assert [r["job_item_id"] for r in json.loads(listed.stdout)] == [item_id]

    resolve = cli.invoke("history", "clear", "deadbeef", "--json")
    assert resolve.exit_code == 1  # a husk is not an item; nothing resolves
    assert json.loads(resolve.stdout)["error"] == "unknown_id"

    swept = cli.invoke("history", "clear", "--all", "--json")
    assert swept.exit_code == 0, swept.stdout
    assert json.loads(swept.stdout)["cleared"] == [item_id]  # husks aren't items
    assert not husk.exists()


def test_remove_husk_retries_a_lingering_handle_then_succeeds(
    cli, document, monkeypatch
):
    """A waiter that grabs the lock file between our release and the
    unlink makes rmdir fail (the file is momentarily back); the husk
    removal retries instead of crashing, and gives up silently rather
    than failing the command."""
    from pathlib import Path

    from ade_cli import history as history_mod

    item_id = parse_file(cli, document)
    real_rmdir = Path.rmdir
    failures = {"left": 2}

    def flaky_rmdir(self):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(13, "Access is denied", str(self))
        return real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", flaky_rmdir)
    sleeps: list[float] = []
    monkeypatch.setattr(history_mod.time, "sleep", sleeps.append)

    result = cli.invoke("history", "clear", item_id, "--json")

    assert result.exit_code == 0, result.stdout
    assert failures["left"] == 0
    assert len(sleeps) == 2  # one backoff per denied attempt
    assert not (cli.home / "jobs" / item_id).exists()


def test_startup_survives_a_stream_whose_reconfigure_raises(monkeypatch):
    """#161's guard is defensive by contract: a stream that *has* a
    reconfigure but refuses it (an embedder's wrapper) must not kill
    startup."""
    from ade_cli import main as main_mod

    class Refusing:
        encoding = "cp1252"

        def reconfigure(self, **kwargs):
            raise ValueError("stream cannot be reconfigured")

    monkeypatch.setattr(main_mod.sys, "stdout", Refusing())
    monkeypatch.setattr(main_mod.sys, "stderr", Refusing())

    main_mod._force_utf8_stdio()  # must simply not raise


def test_concurrent_history_list_invocations_share_the_store_cleanly(
    tmp_path, document
):
    """The QA repro shape (#162), cross-platform: N concurrent processes
    all rewriting the same history.js must every one exit 0 with valid
    JSON. On Windows this is exactly the race that crashed ~6% of calls;
    on POSIX it pins the invariant the retry protects."""
    import concurrent.futures

    home = tmp_path / "shared-home"
    jobs_dir = home / "jobs" / "cafe0123abcd4567"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "meta.json").write_text(
        json.dumps(
            {
                "job_item_id": "cafe0123abcd4567",
                "kind": "parse",
                "state": "parsed",
                "source": str(document),
                "job_id": "job-0001",
                "params": {"model": "dpt-3-pro-latest", "options": {}, "tier": "priority"},
            }
        )
    )
    env = {
        **os.environ,
        "ADE_HOME": str(home),
        "ADE_TELEMETRY": "0",
        "ADE_NO_UPDATE_CHECK": "1",
    }

    def one_call(_):
        return subprocess.run(
            [sys.executable, "-m", "ade_cli", "history", "list", "--json"],
            capture_output=True,
            env=env,
            timeout=120,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one_call, range(8)))

    for result in results:
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        (record,) = json.loads(result.stdout.decode("utf-8"))
        assert record["job_item_id"] == "cafe0123abcd4567"
    # ...and the shared read model came out whole, not torn.
    text = (home / "history.js").read_text(encoding="utf-8")
    assert text.startswith("window.__ADE_HISTORY__ = ")
