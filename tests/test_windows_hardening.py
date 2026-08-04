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
