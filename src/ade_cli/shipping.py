"""Ship usage-ledger events to the platform (#53): opportunistic,
bounded, offline-safe, silent.

After every command's own event is appended, the whole unshipped backlog
ships — the 40th invocation carries events 31–40. Events are partitioned
by the environment they targeted and each partition POSTs to *its own*
environment's ``/v2/telemetry`` with that environment's stored
credential (ADE keys verifiably do not cross environments, so nothing
else could authenticate). A partition without a usable credential stays
buffered for a later run; ``custom``/``unknown`` targets only ship while
an ``ADE_ENDPOINT`` override addresses them. One flush is one POST per
environment: a JSON array of records, each carrying its stable
``idempotent_key`` (minted at record time) and its original timestamp in
epoch seconds. The gateway returns 200 only after every record is
logged; acknowledged rows are then marked ``shipped`` so they never
re-send — the rare lost 200 re-uploads under the same key, filterable
server-side.

Never in the way: the flush runs after the command's output and exit
path are decided, every failure is swallowed, requests carry a short
timeout, and a transport-level failure abandons the remaining
partitions — an offline machine pays one failed connect, not four. The
ledger stays bounded whether or not uploads ever succeed: rotation drops
oldest records first past a size or age cap, and both the shipped-mark
and the rotation rewrite happen under the ledger lock the appender
shares (telemetry.py), so a rewrite can never lose a racing append. The
lock is never held across the network.

Opt-out: ``ADE_TELEMETRY=0`` / ``DO_NOT_TRACK`` disable ledger and
upload alike (telemetry.enabled); ``ADE_TELEMETRY_UPLOAD=0`` disables
the upload alone — the ledger keeps recording locally and rotation keeps
it bounded.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Mapping

import httpx

from . import store, telemetry
from .config import DEFAULT_ENVIRONMENT, ENVIRONMENTS
from .credentials import stored_credential
from .filelock import exclusive
from .gateway import TELEMETRY_PATH, _surface_pairs
from .useragent import user_agent

UPLOAD_PATH = TELEMETRY_PATH
# One flush is one POST per environment; a longer backlog ships across
# later invocations rather than as an unbounded body.
MAX_BATCH_RECORDS = 500
# Rotation caps: past either, oldest records drop first — shipped or not
# — so the ledger stays bounded even offline-forever or opted out of
# uploads.
MAX_LEDGER_BYTES = 256 * 1024
MAX_AGE_DAYS = 30.0
# Deliberately not gateway.REQUEST_TIMEOUT_SECONDS: a telemetry flush
# rides on someone else's invocation and must stay cheap even against a
# black-holed network.
UPLOAD_TIMEOUT_SECONDS = 5.0


def upload_enabled(env: Mapping[str, str]) -> bool:
    """The upload-only knob on top of the ledger opt-out: a user can keep
    the local ledger while never uploading (the test harness shields
    itself the same way)."""
    return telemetry.enabled(env) and env.get("ADE_TELEMETRY_UPLOAD") != "0"


def after_command(
    *,
    command: str,
    argv: list[str],
    env: Mapping[str, str],
    transport: httpx.BaseTransport,
) -> None:
    """The post-command entry point: ship what can ship, then mark and
    rotate. Maintenance runs even when uploads are disabled or nothing
    shipped — the caps hold regardless. Never surfaces, never raises."""
    try:
        if not telemetry.enabled(env):
            return
        home = Path(env["ADE_HOME"]) if env.get("ADE_HOME") else Path.home() / ".ade"
        path = telemetry.ledger_path(home)
        if not path.exists():
            return
        shipped: set[str] = set()
        if upload_enabled(env):
            shipped = _ship(
                path, home, command=command, argv=argv, env=env, transport=transport
            )
        _maintain(path, home, shipped)
    except BaseException:
        pass


# ---------------------------------------------------------------------------
# Shipping


def _ship(
    path: Path,
    home: Path,
    *,
    command: str,
    argv: list[str],
    env: Mapping[str, str],
    transport: httpx.BaseTransport,
) -> set[str]:
    """POST each environment's unshipped records to its own endpoint;
    the returned keys are the acknowledged (200) ones."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for key, record in _load(path):
        if record.get("shipped"):
            continue
        groups.setdefault(str(record.get("env") or "unknown"), []).append((key, record))
    shipped: set[str] = set()
    if not groups:
        return shipped
    headers = {
        "User-Agent": user_agent(*_command_pair(command), *_surface_pairs()),
        "X-Source": "cli",
    }
    for name, rows in groups.items():
        endpoint = _endpoint_for(name, env)
        if endpoint is None:
            continue
        secret = _credential_for(name, home, argv, env)
        if secret is None:
            continue
        batch = rows[:MAX_BATCH_RECORDS]
        try:
            with httpx.Client(
                base_url=endpoint,
                transport=transport,
                timeout=UPLOAD_TIMEOUT_SECONDS,
                headers={**headers, "Authorization": f"Bearer {secret}"},
            ) as client:
                response = client.post(
                    UPLOAD_PATH, json=[_wire(key, record) for key, record in batch]
                )
        except httpx.HTTPError:
            # Transport-level failure: the network is not there. Abandon
            # the remaining partitions — offline must stay cheap.
            break
        # 200 is the only acknowledgment (the gateway logs every record
        # before sending it); anything else leaves the rows buffered for
        # a later attempt.
        if response.status_code == 200:
            shipped.update(key for key, _ in batch)
    return shipped


# Ledger fields that travel; everything else (the shipped mark, future
# local-only bookkeeping) stays home.
_PROPERTY_KEYS = (
    "command",
    "flags",
    "outcome",
    "exit_code",
    "duration_ms",
    "host",
    "term",
    "env",
    "version",
)


def _wire(key: str, record: dict) -> dict:
    """One ledger row as the upload contract wants it: the dedup key, the
    original record time in epoch seconds, everything else in properties."""
    return {
        "idempotent_key": key,
        "ts": int(record.get("ts") or 0),
        "properties": {k: record[k] for k in _PROPERTY_KEYS if k in record},
    }


def _command_pair(command: str) -> tuple[tuple[str, str], ...]:
    """The User-Agent ``command/<name>`` token for the flush request — the
    invoking command's first path segment (the UA grammar is space-
    separated, so ``auth login`` cannot travel whole). The ``(root)``/
    ``(unknown)`` placeholders are not command names and send no token."""
    head = command.split(" ")[0]
    if not head or head.startswith("("):
        return ()
    return (("command", head),)


def _endpoint_for(name: str, env: Mapping[str, str]) -> str | None:
    """Where a partition's events ship: the named environment's endpoint,
    or the live ADE_ENDPOINT override when that is where this partition's
    traffic actually went. ``unknown`` — and ``custom`` without a current
    override — has no shippable address and waits for rotation."""
    override = (env.get("ADE_ENDPOINT") or "").rstrip("/")
    if override and telemetry._ENV_BY_URL.get(override, "custom") == name:
        return override
    return ENVIRONMENTS.get(name)


def _credential_for(
    name: str, home: Path, argv: list[str], env: Mapping[str, str]
) -> str | None:
    """The Bearer secret for a partition, or None to leave it buffered.
    Stored credentials are read per environment (keys do not cross
    environments); the ADE_API_KEY override applies only to the
    invocation's own credential namespace, exactly as the commands
    resolve it. ``custom`` files under that namespace too (ADR-0003:
    ADE_ENDPOINT never changes where credentials live). OAuth tokens are
    used as stored — an expired one is a quiet non-200 and the rows wait
    for a run that refreshed it; a flush never refreshes, never prompts."""
    namespace = _invocation_namespace(argv, env) if name == "custom" else name
    if env.get("ADE_API_KEY") and namespace == _invocation_namespace(argv, env):
        return env["ADE_API_KEY"]
    stored = stored_credential(home, namespace)
    return stored.secret if stored is not None else None


def _invocation_namespace(argv: list[str], env: Mapping[str, str]) -> str:
    """The credential namespace of *this* invocation (``--env`` →
    ``ADE_ENV`` → production), mirroring config.resolve_target without
    its loud validation — an unknown name falls back to the default
    rather than shipping under a typo's namespace."""
    name: str | None = None
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == "--env" and index + 1 < len(argv):
            name = argv[index + 1]
        elif token.startswith("--env="):
            name = token.split("=", 1)[1]
    if name is None:
        name = env.get("ADE_ENV") or None
    return name if name in ENVIRONMENTS else DEFAULT_ENVIRONMENT


# ---------------------------------------------------------------------------
# The ledger file: load, mark shipped, rotate


def _load(path: Path) -> list[tuple[str, dict]]:
    """(key, record) per parseable line. The key is the record's own
    ``idempotent_key`` when it has one; pre-#53 rows get a deterministic
    content hash instead — recomputable on every attempt, so re-sends of
    a legacy row still dedupe, with no rewrite at read time."""
    rows: list[tuple[str, dict]] = []
    for raw in path.read_bytes().splitlines():
        line = raw.strip()
        if not line:
            continue
        record = _parse(line)
        if record is None:
            continue
        rows.append((_line_key(line, record), record))
    return rows


def _line_key(line: bytes, record: dict) -> str:
    key = record.get("idempotent_key")
    if isinstance(key, str) and key:
        return key
    return hashlib.sha256(line).hexdigest()[:32]


def _maintain(path: Path, home: Path, shipped: set[str]) -> None:
    """Mark acknowledged rows and enforce the caps, in one rewrite under
    the ledger lock. Skipped entirely when there is provably nothing to
    do, so the common case costs one stat."""
    if (
        not shipped
        and path.stat().st_size <= MAX_LEDGER_BYTES
        and not _head_expired(path)
    ):
        return
    with exclusive(telemetry.ledger_lock_path(home)):
        # Re-read under the lock: appends since the shipping read are
        # preserved untouched (their keys are not in ``shipped``), and so
        # is every byte the rewrite does not own — an unparseable line
        # travels as-is (never in the way includes never destroying what
        # a human might still want to inspect) until the size cap takes
        # it like any other oldest line.
        lines: list[bytes] = []
        for raw in path.read_bytes().splitlines():
            line = raw.strip()
            if not line:
                continue
            record = _parse(line)
            if record is not None and _line_key(line, record) in shipped:
                record["shipped"] = True
                out = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            else:
                # Only the shipped mark re-encodes a line; everything else —
                # corrupt lines included — travels in its original bytes
                # (stripping is for parsing and key derivation only).
                out = raw
            lines.append(out + b"\n")
        lines = _rotate(lines)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b"".join(lines))
        finally:
            os.close(fd)
        # Same transient-sharing-violation retry as the store's atomic
        # writes (#162): the ledger is one file shared by every
        # concurrent invocation.
        store.replace_with_retry(tmp, path)


def _rotate(lines: list[bytes]) -> list[bytes]:
    """Oldest first: drop expired records, then drop from the front until
    the size cap holds. Line order is record order — the ledger is
    append-only — so the front is always the oldest. A line without a
    readable timestamp never *expires* (age is a claim about a record;
    an unreadable line makes none) but still counts toward — and falls
    to — the size cap."""
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    kept = [line for line in lines if not _expired(line.strip(), cutoff)]
    total = sum(len(line) for line in kept)
    start = 0
    while total > MAX_LEDGER_BYTES and start < len(kept):
        total -= len(kept[start])
        start += 1
    return kept[start:]


def _expired(line: bytes, cutoff: float) -> bool:
    record = _parse(line)
    if record is None:
        return False
    ts = record.get("ts")
    return isinstance(ts, (int, float)) and float(ts) < cutoff


def _parse(line: bytes) -> dict | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _head_expired(path: Path) -> bool:
    """Whether the oldest record is past the age cap — reads one line, so
    the no-op fast path stays cheap."""
    try:
        with path.open("rb") as f:
            head = f.readline().strip()
    except OSError:
        return False
    return _expired(head, time.time() - MAX_AGE_DAYS * 86400)
