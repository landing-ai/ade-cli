"""``history.js`` — the store-level JSONP read model the viewer sidebar
reads (``window.__ADE_HISTORY__``; a plain ``<script>`` include works from
``file://`` where ``fetch()`` does not).

Rewritten by every ``history``/``view`` run from a fresh directory scan, so
manually deleted items drop out and viewer statuses stay current. Because
the file *executes* as script, its payload is emitted only by the strict
serializer below — never string concatenation — and consumers must render
every field as text nodes: store-controlled strings (source paths, schema
names, field values) get no path to becoming markup or code.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import items
from .store import JobStore, write_atomic

ARTIFACT = "history.js"

# The background viewer builder's claim marker inside a job item dir; its
# presence (with a live pid) is what the scan reports as "building".
BUILDING_MARKER = ".viewer.building"


if os.name == "nt":  # pragma: no cover - exercised on Windows only

    def _alive(pid: int) -> bool:
        """Whether ``pid`` is a live process, probed via the Win32 API.

        The POSIX idiom below (``os.kill(pid, 0)``) is doubly unusable
        here (#171): on Windows ``os.kill`` calls TerminateProcess for
        any signal other than the CTRL events — signal 0 included — so
        probing a LIVE builder would kill it mid-build; and probing a
        dead pid raises a bare OSError (WinError 87) rather than
        ProcessLookupError, which crashed every view/history run that
        saw a stale ``.viewer.building`` marker."""
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Explicit signatures: ctypes defaults restype to c_int, which
        # truncates a pointer-sized HANDLE on 64-bit Windows — the probe
        # would then misreport liveness and leak the real handle.
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # Access denied means the pid exists (someone else's process)
            # — a live claim; anything else (invalid parameter for a pid
            # no process holds) means dead.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # exists but unreadable — still a live claim
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

else:

    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True  # exists, owned by someone else — still a live claim
        # Bare OSError included (#171): "no such process" must never crash
        # the scan on a platform that spells it differently than ESRCH.
        except (ProcessLookupError, ValueError, OverflowError, OSError):
            return False
        return True


def viewer_status(store: JobStore, item_id: str) -> str:
    """``built | building | none`` for the sidebar, derived from disk so
    history.js stays a pure projection: the builder's claim marker (with a
    live pid) reads as building; a dead claim is ignored — the next builder
    pass reclaims it."""
    marker = store.item_dir(item_id) / BUILDING_MARKER
    try:
        pid = int(marker.read_text().strip())
    except (FileNotFoundError, ValueError):
        pid = None
    # A torn/empty marker (or pid<=0 — os.kill(0, 0) targets the caller's
    # own process group and "succeeds") is an invalid claim, never a live
    # one: it must not pin the item at "building" forever.
    if pid is not None and pid > 0 and _alive(pid):
        return "building"
    if (store.item_dir(item_id) / "view.html").is_file():
        return "built"
    return "none"


def _credits(store: JobStore, record: dict) -> float | None:
    """Total credits for the item — from meta.json when finalize recorded
    them (the cheap path), else joined from the raw response (fallback for
    items finalized before the field existed)."""
    if isinstance(record.get("credits"), (int, float)):
        return record["credits"]
    raw = store.read_json(
        record["job_item_id"],
        "parse.json" if record["kind"] == "parse" else "extract.json",
    )
    if raw is None:
        return None
    response_meta = raw.get("metadata") or {}
    billing = response_meta.get("billing") or {}
    total = billing.get("total_credits", response_meta.get("credit_usage"))
    return total if isinstance(total, (int, float)) else None


def _model_version(store: JobStore, record: dict) -> str | None:
    if record["kind"] == "parse":
        return record.get("model_version")
    meta = store.read_json(record["job_item_id"], "meta.json") or {}
    return meta.get("model_version") or meta.get("version")


def script_payload(payload: object) -> str:
    """Strict JSON for embedding in script contexts: ``</`` would close the
    carrier ``<script>`` tag from inside a JSON string; ``<\\/`` is the same
    JSON value and inert in HTML."""
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def refresh(store: JobStore, *, now: float) -> Path:
    """Re-scan the store and rewrite ``history.js`` — the one gesture every
    ``history``/``view`` run performs so the sidebar read model heals with
    the listings."""
    return write(store, items.item_records(store), now=now)


def write(store: JobStore, records: list[dict], *, now: float) -> Path:
    """Rewrite ``<store home>/history.js`` from already-scanned records.

    Items are emitted latest submission first — the sidebar renders the
    file in order, and the newest run is the one the user just did.
    ``history list`` keeps its own oldest-first order; timestamp-less
    husks sort last either way.
    """
    ordered = sorted(
        records,
        key=lambda r: (
            r["submitted_at"] is None,
            -(r["submitted_at"] or 0.0),
            r["job_item_id"],
        ),
    )
    payload = {
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "items": [_sidebar_item(store, record) for record in ordered],
    }
    store.home.mkdir(parents=True, exist_ok=True)
    text = "window.__ADE_HISTORY__ = " + script_payload(payload) + ";\n"
    return write_atomic(store.home / ARTIFACT, text)


def _sidebar_item(store: JobStore, record: dict) -> dict:
    item_id = record["job_item_id"]
    viewer = viewer_status(store, item_id)
    ref = record.get("parse") or {}
    item = {
        "id": item_id,
        # What the sidebar row renders (Figma 46:1122): the minimal id that
        # resolves today (hover = full), the model family, credits, and the
        # timestamps the compact time formats from.
        "short_id": items.short_id(store, item_id),
        "kind": record["kind"],
        "state": record["state"],
        "reason": record.get("reason"),
        "source": record["source"],
        "source_name": items.source_name(record["source"]),
        "params": items.compact_params(record),
        # Which environment the run addressed; the sidebar renders it after
        # the id. Absent (null) on items from before the field existed.
        "environment": record.get("environment"),
        "model_version": _model_version(store, record),
        "credits": _credits(store, record),
        "submitted_at": record.get("submitted_at"),
        "completed_at": record.get("completed_at"),
        # Parent linkage: referencing extracts render beneath their parse.
        # An orphan (parse manually deleted) lists as a top-level item, the
        # same degradation `history list` renders — a parent id that no
        # longer exists would make consumers mis-group or drop it.
        "parent": None if ref.get("missing") else ref.get("job_item_id"),
        # Stale extraction (its parse was --force re-run): the sidebar
        # renders an amber mark with the explanation on hover.
        "stale": bool(record.get("stale")),
        "viewer": viewer,
        # Store-relative, so a sidebar loaded from any jobs/<id>/view.html
        # can navigate to its siblings.
        "href": f"jobs/{item_id}/view.html" if viewer == "built" else None,
    }
    if ref.get("direct"):
        # Provenance: a direct `extract -d` created its parse (data-only —
        # no UI badge yet).
        item["direct"] = True
    if record.get("schema_violation_error"):
        # Partial extraction (#118): strict=false skipped schema fields
        # (data-only — no UI badge yet).
        item["schema_violation_error"] = record["schema_violation_error"]
    if record.get("warnings"):
        # Server-warning count (#118); the messages stay verbatim in the
        # item's extract.json.
        item["warnings"] = record["warnings"]
    return item
