"""The local store: ``$ADE_HOME/jobs/<job-item-id>/``.

The job item id is the CLI's local primary key — one id per invocation
identity: *verb × which environment ran it × where the document lives ×
what its bytes are × how it was processed*. Any component differing mints
a sibling item; the store is fully flat (parse and extract items side by
side under ``jobs/``). Raw API responses are ground truth and never
edited.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .filelock import exclusive


# Truncated SHA-256 is plenty for a per-machine store; prefixes stay short
# enough to type while collisions stay out of practical reach.
JOB_ITEM_ID_HEX_CHARS = 16


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_params(params: dict) -> str:
    """The canonical JSON form params are hashed over: sorted keys, compact
    separators, no ASCII escaping. Callers pass fully-resolved params
    (defaults inlined), so a changed default mints new identities loudly
    rather than silently re-keying old ones."""
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def params_hash(params: dict) -> str:
    return _sha256(canonical_params(params).encode())


def local_identity(path: Path, content: bytes) -> dict:
    """The identity components of a local source: where the document lives
    and what its bytes are. For ``--markdown FILE`` items the markdown bytes
    are the content."""
    return {
        "source_hash": _sha256(str(path.resolve()).encode()),
        "content_hash": _sha256(content),
    }


def url_identity(url: str) -> dict:
    """URL sources have no content component — the CLI never sees the bytes
    before submit; identity is the URL × params, and ``--force`` is the
    refresh gesture for remote drift."""
    return {"url_hash": _sha256(url.encode())}


def derive_id(verb: str, environment: str, identity: dict, params: dict) -> str:
    """The job item id: ``sha256(verb:environment:source:content:params)[:16]``
    for local sources, ``sha256(verb:environment:url:params)[:16]`` for URL
    sources. The verb prefix keeps parse and extract ids from ever
    colliding; the environment component keeps one environment's result
    from ever serving another's request (jobs and their server-side ids
    are per-environment); ``identity`` is reusable verbatim from a stored
    item's meta.json (extract derives its id from the identity of the
    parse item it references, in that item's environment)."""
    if "url_hash" in identity:
        components = (verb, environment, identity["url_hash"], params_hash(params))
    else:
        components = (
            verb,
            environment,
            identity["source_hash"],
            identity["content_hash"],
            params_hash(params),
        )
    return _sha256(":".join(components).encode())[:JOB_ITEM_ID_HEX_CHARS]


@dataclass(frozen=True)
class JobStore:
    home: Path

    @property
    def jobs_root(self) -> Path:
        return self.home / "jobs"

    def item_dir(self, item_id: str) -> Path:
        return self.jobs_root / item_id

    def read_json(self, item_id: str, name: str) -> dict | None:
        # Single read, no exists() pre-check: a concurrent steal_if() can
        # remove the file between the two calls.
        try:
            return json.loads(
                (self.item_dir(item_id) / name).read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None

    def write_json(self, item_id: str, name: str, payload: object) -> Path:
        # name may be a nested slot (a referencing extract's parse/ref.json);
        # parents are created either way.
        path = self.item_dir(item_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # ensure_ascii=False keeps raw artifacts faithful to the response
        # (no \uXXXX escapes in stored markdown or values); explicit UTF-8 so
        # persistence never depends on the platform locale.
        return write_atomic(
            path, json.dumps(payload, indent=2, ensure_ascii=False)
        )

    @contextmanager
    def lock(self, item_id: str) -> Iterator[None]:
        """Interprocess mutex for this item's claim-ticket transitions (and
        live-artifact publication). flock is advisory, which suffices: every
        mutator is this CLI. Never hold it across network calls.

        Acquisition retries when the directory vanishes between the mkdir
        and the lock-file open — ``history clear`` deletes item dirs (the
        lock file last, after release; see #160), and that window must
        surface to a racing mutator as a clean re-acquire on a recreated
        dir, never a FileNotFoundError crash. Bounded: two live processes
        cannot ping-pong forever (clear deletes each dir once)."""
        d = self.item_dir(item_id)
        for _ in range(100):
            d.mkdir(parents=True, exist_ok=True)
            acquired = exclusive(d / ".ticket.lock")
            try:
                acquired.__enter__()
            except FileNotFoundError:
                continue
            break
        else:  # pragma: no cover - would need 100 perfectly timed clears
            raise FileNotFoundError(
                f"could not acquire the item lock for {item_id}: the "
                "directory kept disappearing during acquisition"
            )
        try:
            yield
        finally:
            acquired.__exit__(None, None, None)

    @contextmanager
    def store_lock(self) -> Iterator[None]:
        """Interprocess mutex over the whole store — held by ``history
        clear`` so the dependent scan and the deletions it drives are one
        atomic sweep against any concurrent clear. Same advisory posture as
        the item lock; never held across network calls."""
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        with exclusive(self.jobs_root / ".store.lock"):
            yield

    def claim(self, item_id: str, name: str, payload: object) -> bool:
        """Create ``name`` iff the slot is empty. Serialized by lock(); the
        record is complete when visible (atomic tmp+rename write)."""
        with self.lock(item_id):
            if (self.item_dir(item_id) / name).exists():
                return False
            self.write_json(item_id, name, payload)
            return True

    def cas(self, item_id: str, name: str, expected: dict, new: dict | None) -> bool:
        """Compare-and-swap ``name`` under the item lock: publish ``new``
        (or remove, when None) only while the current content still equals
        ``expected``. A record that changed hands is never unpublished, not
        even transiently. False means the slot moved on without us."""
        with self.lock(item_id):
            if self.read_json(item_id, name) != expected:
                return False
            if new is None:
                (self.item_dir(item_id) / name).unlink(missing_ok=True)
            else:
                self.write_json(item_id, name, new)
            return True

    def write_text(self, item_id: str, name: str, text: str) -> Path:
        path = self.item_dir(item_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return write_atomic(path, text)


def write_atomic(path: Path, text: str) -> Path:
    """Temp-write + rename: a crash mid-write can never leave a truncated
    record where readers expect a complete one. The temp name carries pid
    AND thread id — the lease heartbeat writes from a second thread of the
    same process, so pid alone could collide."""
    tmp = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    tmp.write_text(text, encoding="utf-8")
    replace_with_retry(tmp, path)
    return path


# ~0.6s of total patience: enough to outlast another process's momentary
# open of the destination, short enough that a genuinely locked file
# (never seen in practice) still errors promptly.
_REPLACE_ATTEMPTS = 10


def replace_with_retry(tmp: Path, path: Path) -> None:
    """``os.replace``, retried briefly with backoff on PermissionError
    (#162). On Windows, MoveFileEx fails with ERROR_ACCESS_DENIED while
    the destination is momentarily open in another process without
    share-delete access — and every ade invocation rewrites the same
    ``history.js``, so two concurrent invocations race on exactly this
    call. The condition is transient by nature; tens of milliseconds of
    backoff clears it. POSIX renames never fail this way, so the retry
    path is Windows-only in practice. Shared by every temp-then-replace
    publisher of a store-shared file (history.js here, the telemetry
    ledger, the update-check stamp)."""
    delay = 0.01
    for _ in range(_REPLACE_ATTEMPTS - 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
    os.replace(tmp, path)
