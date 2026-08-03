"""Test harness: every test drives real commands through the CLI seam.

One primary seam — typer's in-process runner against a temp ``ADE_HOME`` —
with the HTTP transport and clock injected as fakes. Tests assert only
external behavior: exit codes, stdout payloads, and store contents on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
import pytest
from typer.testing import CliRunner

from ade_cli import surface
from ade_cli.main import app
from ade_cli.ports import Ports


def _no_browser(url: str) -> bool:
    raise AssertionError(f"unscripted browser open: {url}")


def _no_getchar() -> str:
    raise AssertionError("unscripted raw key read")


def _scripted_keys(keys: list[str | BaseException]) -> Callable[[], str]:
    """A raw-key reader that plays back a script — an exception entry is
    raised in place of a key (e.g. OSError for a raw-mode failure). Running
    dry is a harness failure, mirroring the transport and browser fakes."""
    remaining = iter(keys)

    def read() -> str:
        try:
            key = next(remaining)
        except StopIteration:
            raise AssertionError("ran out of scripted keys") from None
        if isinstance(key, BaseException):
            raise key
        return key

    return read


class FakeTransport(httpx.BaseTransport):
    """Scripted gateway. Any unscripted request is a harness failure —
    the offline suite must never reach the network."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._script: list[Callable[[httpx.Request], httpx.Response]] = []

    def respond_with(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._script.append(handler)

    def respond(
        self,
        status_code: int,
        json_body: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._script.append(
            lambda request: httpx.Response(status_code, json=json_body, headers=headers)
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()  # materialize streamed (multipart) bodies for assertions
        self.requests.append(request)
        if not self._script:
            raise AssertionError(
                f"unscripted network call: {request.method} {request.url}"
            )
        return self._script.pop(0)(request)


class FakeClock:
    def __init__(self, start: float = 1_750_000_000.0) -> None:
        self._now = start
        self.sleeps: list[float] = []
        self.interrupt_sleep_at: int | None = None  # simulate Ctrl-C on nth sleep

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if self.interrupt_sleep_at is not None and len(self.sleeps) >= self.interrupt_sleep_at:
            raise KeyboardInterrupt
        self.sleeps.append(seconds)
        self._now += seconds


@dataclass
class Cli:
    home: Path
    transport: FakeTransport
    clock: FakeClock
    # Injected terminal-ness: the runner's captured streams are never real
    # ttys, so tty-only rendering (progress lines, tables) is opted into
    # per-test. False (not None/detect) keeps tests hermetic under any
    # runner. stdin_tty additionally gates the arrow-key selector, whose
    # raw keys are scripted per-call via ``keys``.
    stdout_tty: bool = False
    stderr_tty: bool = False
    stdin_tty: bool = False
    # Per-test env baseline applied after the ambient shield (a test file
    # opts a whole suite into an env var here).
    env_defaults: dict[str, str | None] = field(default_factory=dict)
    # typer's runner keeps stderr separate: progress rendering (#33) is
    # asserted on its own stream (result.stderr), and stdout byte-stability
    # is a real assertion, not a mix.
    _runner: CliRunner = field(default_factory=CliRunner)

    def invoke(
        self,
        *args: str,
        input: str | None = None,
        env: dict[str, str | None] | None = None,
        browser: Callable[[str], bool] | None = None,
        keys: list[str | BaseException] | None = None,
    ):
        # Pin ADE_HOME to the temp store and shield the run from ambient
        # ADE_* vars and surface markers (the machine running the tests
        # may itself be an agent host or a CI runner — its markers must
        # not leak into ledger events); tests opt back in per-call via
        # ``env``.
        merged: dict[str, str | None] = {
            "ADE_HOME": str(self.home),
            "ADE_API_KEY": None,
            "ADE_ENDPOINT": None,
            "ADE_ENV": None,
            "ADE_TELEMETRY": None,
            # Ledger *shipping* (#53) stays off in the harness: the ledger
            # itself records normally (tests above opt out per-call), but
            # the post-command flush would otherwise fire an unscripted
            # POST after every invocation. Flush tests opt back in with
            # env={"ADE_TELEMETRY_UPLOAD": None}.
            "ADE_TELEMETRY_UPLOAD": "0",
            # The periodic update check (#138) stays off the same way — it
            # would fire an unscripted GET after any stderr-tty invocation.
            # Nudge tests opt back in with env={"ADE_NO_UPDATE_CHECK": None}.
            "ADE_NO_UPDATE_CHECK": "1",
            "DO_NOT_TRACK": None,
        }
        merged.update(dict.fromkeys(surface.marker_variables()))
        merged.update(self.env_defaults)
        if env:
            merged.update(env)
        return self._runner.invoke(
            app,
            list(args),
            input=input,
            env=merged,
            obj=Ports(
                transport=self.transport,
                clock=self.clock,
                browser=browser or _no_browser,
                getchar=_scripted_keys(keys) if keys is not None else _no_getchar,
                stdout_tty=self.stdout_tty,
                stderr_tty=self.stderr_tty,
                stdin_tty=self.stdin_tty,
            ),
        )


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    return Cli(
        home=tmp_path / "ade-home",
        transport=FakeTransport(),
        clock=FakeClock(),
    )
