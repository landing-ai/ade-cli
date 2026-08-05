"""Attached document copies for URL-parsed job items (#169).

``parse --document-url`` never hands the CLI the document bytes — the
server fetches the URL — so page previews and crops have nothing local
to render from. An *attached copy* closes that gap on explicit consent:
``parse --keep-copy`` downloads the document at parse time (while the
URL — often pre-signed — still works), and ``view --download`` fetches
it after the fact. The copy lives inside the job item
(``jobs/<id>/document.<ext>``) and is recorded on meta.json as auxiliary
metadata: the recorded ``source`` stays the URL (provenance truth) and
the item id never moves. The raster layer falls back to the copy via
``renderable_source``.

The CLI still never fetches a URL without one of these explicit flags —
and because it never saw the original bytes, an attached copy is not
verifiable against the parsed generation: renders from it carry the
``caveat`` note rather than posing as ground truth.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .store import JobStore, replace_with_retry

# Extensions preserved on the copy's filename — a display nicety only;
# the renderer sniffs bytes, never the suffix.
_KNOWN_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# A copy exists to be rastered locally; past this it is likelier a wrong
# URL than a document.
MAX_COPY_BYTES = 512 * 1024 * 1024

_URL_PREFIXES = ("http://", "https://")


class AttachError(Exception):
    """A copy that could not be attached; ``kind`` is the machine code."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def is_url_source(meta: dict | None) -> bool:
    source = (meta or {}).get("source") or ""
    return source.startswith(_URL_PREFIXES)


def copy_name(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return f"document{suffix if suffix in _KNOWN_SUFFIXES else ''}"


def attached_file(jobs: JobStore, item_id: str, meta: dict | None) -> Path | None:
    """The attached copy's path, when one is recorded AND still on disk —
    a manually deleted copy degrades back to the URL messaging."""
    name = (meta or {}).get("attached_source")
    if not name:
        return None
    path = jobs.item_dir(item_id) / name
    return path if path.is_file() else None


def renderable_source(jobs: JobStore, item_id: str, meta: dict | None) -> str | None:
    """What page imagery renders from: the recorded source verbatim for
    local items (missing files keep their honest notes), the attached
    copy for URL items that have one, else the URL itself (whose note
    names the --download remediation)."""
    source = (meta or {}).get("source")
    if source and source.startswith(_URL_PREFIXES):
        attached = attached_file(jobs, item_id, meta)
        if attached is not None:
            return str(attached)
    return source


# The full story behind the caveat — the viewer shows it on hover (like
# the stale badge); the CLI surfaces carry only the short, actionable
# form below.
CAVEAT_DETAIL = (
    "This item was parsed from a URL: the server fetched and read the "
    "original document, and the CLI never received those bytes. The copy "
    "behind these page images was downloaded separately — almost "
    "certainly identical, but there is nothing to verify it against. "
    "Markdown, elements, and extractions come straight from the parse "
    "and are unaffected either way. If boxes ever look misaligned with "
    "a page image, the remote document likely changed since the parse — "
    "re-parse the downloaded file locally (ade parse -d <file>) for a "
    "fully verifiable item."
)


def caveat(jobs: JobStore, item_id: str, meta: dict | None) -> str | None:
    """The short, actionable form of the attached-copy caveat, for the
    surfaces with no hover (the CLI note line, crop warnings). Calm by
    design: the copy is almost always right — the note says what to do
    in the one case it isn't."""
    if not is_url_source(meta) or attached_file(jobs, item_id, meta) is None:
        return None
    return (
        "page imagery renders from the item's downloaded copy of the URL; "
        "if boxes ever look misaligned, re-parse the downloaded file "
        "locally (ade parse -d <file>)"
    )


def download(
    jobs: JobStore,
    item_id: str,
    meta: dict,
    *,
    transport: httpx.BaseTransport,
    now: float,
) -> tuple[str, int]:
    """Fetch the item's URL source and attach the copy; returns
    ``(filename, bytes)``. Raises AttachError with the remediation —
    pre-signed URLs expire, so a late fetch failing is the expected
    failure mode, not a surprise."""
    url = meta.get("source") or ""
    name = copy_name(url)
    target = jobs.item_dir(item_id) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{name}.tmp-{os.getpid()}")
    digest = hashlib.sha256()
    received = 0
    # Streamed to disk, never buffered whole: the size cap must reject a
    # wrong URL as soon as it is exceeded (or up front, when the server
    # declares Content-Length), not after the entire body sat in memory.
    try:
        with httpx.Client(
            transport=transport, follow_redirects=True, timeout=60.0
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise AttachError(
                        "download_failed",
                        f"{url} answered HTTP {response.status_code} — "
                        "pre-signed URLs expire, so the link that fed this "
                        "parse may no longer serve the document. Download "
                        "it by other means and parse the local file "
                        "(ade parse -d <file>), or re-parse with a fresh "
                        "URL and --keep-copy.",
                    )
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_COPY_BYTES:
                    raise AttachError(
                        "download_failed",
                        f"{url} declares {declared} bytes — past the "
                        f"{MAX_COPY_BYTES}-byte attach cap; parse the local "
                        "file instead (ade parse -d <file>).",
                    )
                with tmp.open("wb") as sink:
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > MAX_COPY_BYTES:
                            raise AttachError(
                                "download_failed",
                                f"{url} exceeded the {MAX_COPY_BYTES}-byte "
                                "attach cap mid-download; parse the local "
                                "file instead (ade parse -d <file>).",
                            )
                        digest.update(chunk)
                        sink.write(chunk)
    except httpx.HTTPError as error:
        tmp.unlink(missing_ok=True)
        raise AttachError(
            "download_failed",
            f"could not fetch {url}: {type(error).__name__}: {error}. "
            "Download the document by other means and parse the local "
            "file (ade parse -d <file>).",
        ) from error
    except AttachError:
        tmp.unlink(missing_ok=True)
        raise
    if received == 0:
        tmp.unlink(missing_ok=True)
        raise AttachError(
            "download_failed", f"{url} answered an empty body; nothing to attach."
        )
    replace_with_retry(tmp, target)
    # Record on the commit record under the item lock — a read-modify-write
    # against whatever generation currently owns meta.json.
    with jobs.lock(item_id):
        current = jobs.read_json(item_id, "meta.json") or {}
        current.update(
            {
                "attached_source": name,
                "attached_sha256": digest.hexdigest(),
                "attached_at": now,
            }
        )
        jobs.write_json(item_id, "meta.json", current)
    return name, received
