"""Injected ports: the HTTP transport, the clock, terminal-ness, the
browser opener, the raw-key reader, PATH lookup, and the attached
subprocess runner.

Production builds real ones; tests inject fakes through the CLI seam
(``ctx.obj``). No command in this slice performs HTTP, but every command
runs against these ports so the whole build shares one harness.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import httpx


class Clock(Protocol):
    def now(self) -> float:
        """Wall-clock time (timestamps for humans/bookkeeping)."""
        ...

    def monotonic(self) -> float:
        """Monotonic time (budgets and deadlines; immune to clock jumps)."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def open_browser(url: str) -> bool:
    """True only when a browser was plausibly launched; False sends the
    caller to the ``--api-key`` remediation (headless boxes, SSH)."""
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def run_attached(argv: list[str], cwd: Path | None = None) -> int:
    """Run a foreground child that inherits this process's stdio — the
    interactive agent session and the extract re-exec both need the real
    terminal, so nothing is captured or redirected."""
    return subprocess.run(argv, cwd=cwd).returncode


@dataclass
class Ports:
    transport: httpx.BaseTransport = field(default_factory=httpx.HTTPTransport)
    clock: Clock = field(default_factory=SystemClock)
    browser: Callable[[str], bool] = open_browser
    # Raw single-key reader for arrow-key menus. None means typer.getchar
    # (the vendored click getchar — real raw-mode reads); tests inject a
    # scripted reader.
    getchar: Callable[[], str] | None = None
    # PATH lookup for agent-CLI detection; tests inject a fake resolving
    # only the names the test declares present.
    which: Callable[[str], str | None] = shutil.which
    # Foreground subprocess inheriting this process's stdio, returning the
    # exit code; tests inject a recorder — the suite never launches a
    # real process.
    run_agent: Callable[..., int] = run_attached
    # Terminal-ness of the real streams, injectable because a test runner's
    # captured streams are never ttys. None means detect.
    stdout_tty: bool | None = None
    stderr_tty: bool | None = None
    stdin_tty: bool | None = None

    def stdout_is_tty(self) -> bool:
        if self.stdout_tty is not None:
            return self.stdout_tty
        return sys.stdout.isatty()

    def stderr_is_tty(self) -> bool:
        if self.stderr_tty is not None:
            return self.stderr_tty
        return sys.stderr.isatty()

    def stdin_is_tty(self) -> bool:
        if self.stdin_tty is not None:
            return self.stdin_tty
        return sys.stdin.isatty()
