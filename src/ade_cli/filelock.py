"""Cross-platform advisory file lock and stream probing — the one module
allowed to import platform-specific primitives (fcntl on POSIX, msvcrt on
Windows), so the frozen binary starts everywhere. Same advisory posture
as before: every mutator is this CLI.

Besides the lock, this hosts ``stdin_ready`` (#168): "is reading stdin
guaranteed not to block?", answered by select() on POSIX and
PeekNamedPipe on Windows — select() there supports only sockets, so
probing piped stdin raised WinError 10093 every time and the documented
``echo $KEY | ade auth login`` pattern was silently never detected."""

from __future__ import annotations

import os
import select
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

# Poll cadence of the Windows pipe probe — PeekNamedPipe has no built-in
# wait, so readiness is sampled inside the same timeout budget select()
# spends on POSIX.
_PIPE_POLL_SECONDS = 0.01


def poll_until_ready(
    probe: Callable[[], int | None],
    *,
    timeout: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Sample ``probe`` until a stream is safely readable or ``timeout``
    passes. ``probe`` reports bytes waiting, or None when the stream has
    ended (a broken pipe / EOF — reading returns immediately, so that
    counts as ready). The pure half of the Windows probe, kept separate
    so its decision table is testable on every platform."""
    deadline = monotonic() + timeout
    while True:
        available = probe()
        if available is None or available > 0:
            return True
        if monotonic() >= deadline:
            return False
        sleep(_PIPE_POLL_SECONDS)

if os.name == "nt":
    import msvcrt

    def stdin_ready(fileno: int, *, timeout: float) -> bool:  # pragma: no cover
        """Whether reading ``fileno`` cannot block, via PeekNamedPipe.

        Files are always readable; anonymous pipes are peeked without
        consuming; console handles never reach here (tty callers bail
        first). Windows-only body — covered by the Windows QA suite; the
        decision table lives in poll_until_ready above.
        """
        import ctypes
        from ctypes import wintypes

        FILE_TYPE_DISK = 0x0001
        FILE_TYPE_PIPE = 0x0003
        try:
            handle = msvcrt.get_osfhandle(fileno)
        except OSError:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Explicit signatures (same reason as historyjs._alive): a
        # pointer-sized HANDLE must not pass through ctypes' c_int
        # defaults on 64-bit Windows.
        kernel32.GetFileType.restype = wintypes.DWORD
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        kernel32.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        file_type = kernel32.GetFileType(wintypes.HANDLE(handle))
        if file_type == FILE_TYPE_DISK:
            return True  # redirected from a file: reads never block
        if file_type != FILE_TYPE_PIPE:
            return False  # char device/unknown: nothing was piped

        def probe() -> int | None:
            available = wintypes.DWORD()
            ok = kernel32.PeekNamedPipe(
                wintypes.HANDLE(handle),
                None, 0, None,
                ctypes.byref(available),
                None,
            )
            if not ok:
                return None  # broken pipe: the writer is gone, read = EOF
            return available.value

        return poll_until_ready(probe, timeout=timeout)

    @contextmanager
    def exclusive(path: Path) -> Iterator[None]:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            # LK_LOCK gives up after ~10s; loop for flock-like indefinite
            # blocking (the refresh lock is legitimately held across a
            # token-refresh network call). It sleeps ~1s per attempt
            # internally, so the loop isn't hot.
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                # Unlock the same byte range: the fd is never written, but
                # re-seek to 0 so the range can't drift.
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

else:
    import fcntl

    def stdin_ready(fileno: int, *, timeout: float) -> bool:
        """Whether reading ``fileno`` cannot block — select(), the POSIX
        answer."""
        try:
            return bool(select.select([fileno], [], [], timeout)[0])
        except OSError:
            return False

    @contextmanager
    def exclusive(path: Path) -> Iterator[None]:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
