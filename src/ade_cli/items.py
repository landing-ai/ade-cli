"""Job-item id resolution and per-item read-model records.

Commands that read the store take a job item id or an unambiguous prefix
— nothing else: with params inside identity, a path or source name may
legitimately match several sibling items, so path lookup is not a
resolution rule (the remediation is ``history list``, or the convenience
verbs that accept paths outright). Records derive from tickets and
artifacts on disk — nothing here performs HTTP.
"""

from __future__ import annotations

from pathlib import Path

from .store import JobStore


class IdError(Exception):
    """A job item id (or prefix) that did not resolve to exactly one item."""

    def __init__(self, token: str, kind: str, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.token = token
        self.kind = kind  # "unknown_id" | "ambiguous_id"
        self.message = message
        self.candidates = candidates or []


def resolve(store: JobStore, token: str) -> str:
    """Resolve a job item id or unambiguous prefix to exactly one stored
    item. Unknown or ambiguous tokens error with candidates listed."""
    ids = item_ids(store)
    if token in ids:
        return token
    prefixed = [item_id for item_id in ids if item_id.startswith(token)]
    if len(prefixed) == 1:
        return prefixed[0]
    if len(prefixed) > 1:
        raise IdError(
            token,
            "ambiguous_id",
            f"{token!r} is a prefix of {len(prefixed)} job item ids; use more characters.",
            candidates=prefixed,
        )
    raise IdError(
        token,
        "unknown_id",
        f"No stored job item matches {token!r}; run `ade history list` "
        "to see the store.",
    )


SHORT_ID_FLOOR = 8


def short_id(store: JobStore, item_id: str) -> str:
    """A short id prefix that resolves unambiguously today — what the
    ``next:``/``open:`` hint lines teach. Floored at ``SHORT_ID_FLOOR``
    characters so hints stay stable-looking and survive a store that grows
    a few more items."""
    others = [other for other in item_ids(store) if other != item_id]
    for length in range(SHORT_ID_FLOOR, len(item_id)):
        prefix = item_id[:length]
        if not any(other.startswith(prefix) for other in others):
            return prefix
    return item_id


def item_ids(store: JobStore) -> list[str]:
    """Job item ids present in the store, in stable (sorted) order — every
    listing re-derives this from a directory scan, so manually deleted
    items simply vanish. A directory with neither a claim ticket nor
    metadata is a husk, not an item; a symlink is never one — following it
    would take store operations outside the store."""
    root = store.jobs_root
    if not root.is_dir():
        return []
    return sorted(
        entry.name for entry in root.iterdir() if _is_item_dir(entry)
    )


def _is_item_dir(path: Path) -> bool:
    """Whether a directory is a real job item: holds a claim ticket or
    metadata, and is never a symlink — following one would take store
    operations outside the store."""
    return (
        path.is_dir()
        and not path.is_symlink()
        and ((path / "meta.json").exists() or (path / "job.json").exists())
    )


def parse_ref(store: JobStore, item_id: str) -> dict | None:
    """A referencing extract item's linkage record (``parse/ref.json``):
    ``{job_item_id, parse_job_id}`` naming the parse item it ran against.
    None for parse items and non-referencing extract items."""
    return store.read_json(item_id, "parse/ref.json")


def referencing_extracts(store: JobStore, parse_item_id: str) -> list[str]:
    """The extract items whose ``parse/ref.json`` names this parse item —
    the dependency edge ``history clear`` cascades over and ``view`` renders
    as extraction layers. Scan-derived, so it heals after manual deletion."""
    return [
        item_id
        for item_id in item_ids(store)
        if (parse_ref(store, item_id) or {}).get("job_item_id") == parse_item_id
    ]


def item_record(store: JobStore, item_id: str) -> dict:
    """The stable read-model record for one job item.

    State follows the current guarantee: a pending or failed claim ticket
    describes the newest intent and wins over older completed metadata; the
    record's per-run fields then come from the same generation as the state
    (null where that generation has none yet).
    """
    ticket = store.read_json(item_id, "job.json") or {}
    meta = store.read_json(item_id, "meta.json") or {}
    kind = meta.get("kind") or ticket.get("kind") or "parse"
    done = "extracted" if kind == "extract" else "parsed"
    state = _guarantee_state(ticket, meta, done=done)
    current = ticket if state in ("pending", "failed", "unreadable") else meta
    other = ticket if current is meta else meta
    record = {
        "job_item_id": item_id,
        "kind": kind,
        "state": state,
        # Provenance follows the state's generation too; the other record is
        # only a fallback (tickets written before source was recorded).
        "source": current.get("source") or other.get("source"),
        # Why an unreadable ticket couldn't be read (None elsewhere): the
        # diagnosis recorded when the guarantee rejected the result.
        "reason": current.get("reason"),
        "job_id": current.get("job_id"),
        "params": current.get("params"),
        # Which environment the run addressed (part of the item id since
        # ADR-0003); falls back like source for records predating the field.
        "environment": current.get("environment") or other.get("environment"),
        "credits": current.get("credits"),
        "submitted_at": ticket.get("submitted_at"),
        "completed_at": meta.get("completed_at"),
        "artifacts": artifact_index(store, item_id),
    }
    if kind == "parse":
        record["model_version"] = current.get("model_version")
        record["page_count"] = current.get("page_count")
        record["failed_pages"] = current.get("failed_pages")
    else:
        record["fields"] = sorted((meta.get("schema") or {}).get("properties") or [])
        # The partial-success markers (#118): the violation message and the
        # server-warning count — read from the commit record, so listings
        # can say so without opening extract.json (which keeps the full
        # warnings verbatim). None on clean or pre-#118 items.
        record["schema_violation_error"] = meta.get("schema_violation_error")
        record["warnings"] = meta.get("warnings")
        ref = parse_ref(store, item_id)
        if ref is not None:
            # Parent linkage; a manually deleted parse degrades to an
            # explicit parse-missing state on the next scan, never a
            # dangling render. Checked against the referenced directory
            # itself — a full rescan here would make listing N items O(N²).
            parse_item_id = ref.get("job_item_id")
            record["parse"] = {
                **ref,
                "missing": not (
                    parse_item_id
                    and _is_item_dir(store.item_dir(parse_item_id))
                ),
            }
        else:
            record["parse"] = None
    return record


def item_records(store: JobStore) -> list[dict]:
    """All items' records in history order: oldest submission first (ties
    and timestamp-less husks by id) — the flat answer to "what runs did I
    do here". Re-scanned every call; zero HTTP."""
    records = [item_record(store, item_id) for item_id in item_ids(store)]
    records.sort(
        key=lambda r: (
            r["submitted_at"] is None,
            r["submitted_at"] or 0.0,
            r["job_item_id"],
        )
    )
    return records


def live_parse(store: JobStore, item_id: str) -> tuple[dict, dict] | None:
    """The item's completed parse as ``(meta, raw response)``, or None when
    no generation-consistent parse is stored."""
    return _live_result(store, item_id, done="parsed", artifact="parse.json")


def latest_parse(
    store: JobStore, identity: dict, environment: str
) -> tuple[str, dict, dict] | None:
    """The reuse scan behind ``extract -d``: the newest completed parse of
    this exact source path + content *in this environment*, as
    ``(item_id, meta, raw response)`` — or None, which means the caller
    runs a parse job first.

    The reuse pool is all ``kind: parse`` job items of the target
    environment (decision 10, revised — every parse the CLI runs is a
    top-level, reusable item; environments never serve each other, since
    the extract will reference this parse's server-side job id). Any
    params variant qualifies; newest ``completed_at`` wins, and only a
    generation-consistent (live) parse is returned.
    """
    candidates = [
        (meta.get("completed_at") or 0.0, item_id)
        for item_id in item_ids(store)
        if (meta := store.read_json(item_id, "meta.json")) is not None
        and meta.get("kind") == "parse"
        and meta.get("state") == "parsed"
        and meta.get("identity") == identity
        and meta.get("environment") == environment
    ]
    for _, item_id in sorted(candidates, reverse=True):
        live = live_parse(store, item_id)
        if live is not None:
            return (item_id, *live)
    return None


def live_extract(store: JobStore, item_id: str) -> tuple[dict, dict] | None:
    """One extract item's completed result as ``(meta, raw response)``, or
    None when no generation-consistent extraction is stored."""
    return _live_result(store, item_id, done="extracted", artifact="extract.json")


def _live_result(
    store: JobStore, item_id: str, *, done: str, artifact: str
) -> tuple[dict, dict] | None:
    """The generation-consistency gate both kinds share: meta.json is the
    commit record but vouches only for the raw response written with it —
    a crash mid-publish can strand one generation's response beside
    another's metadata — so the pair serves only while their job_ids
    agree."""
    meta = store.read_json(item_id, "meta.json")
    if meta is None or meta.get("state") != done:
        return None
    response = store.read_json(item_id, artifact)
    if (
        response is None
        or response.get("metadata", {}).get("job_id") != meta.get("job_id")
    ):
        return None
    return meta, response


def _guarantee_state(ticket: dict, meta: dict, *, done: str) -> str:
    """The lifecycle state a ticket/commit-record pair describes: a pending,
    failed, or unreadable claim ticket is the newest intent and wins over
    older completed metadata; a commit record in the ``done`` state is
    served as-is; anything else (husk, mid-publish crash) reads as
    pending."""
    if ticket.get("state") == "pending":
        return "pending"
    if ticket.get("state") in ("failed", "cancelled"):
        return "failed"
    if ticket.get("state") == "unreadable":
        return "unreadable"
    if meta.get("state") == done:
        return meta["state"]
    return "pending"


def artifact_index(store: JobStore, item_id: str) -> list[dict]:
    """The item's artifacts on disk, by name. Dotfiles (the lock file,
    atomic-write temps) are plumbing, not artifacts; symlinks are excluded
    so the index can never reach outside the item directory."""
    return [
        {"name": entry.name, "bytes": entry.stat().st_size}
        for entry in sorted(store.item_dir(item_id).iterdir())
        if entry.is_file()
        and not entry.is_symlink()
        and not entry.name.startswith(".")
    ]


# Caps on the extract field summary: parse params are naturally short
# (model · tier) but extract params scale with the schema, and a wide
# schema must never own the row it annotates (#144) — at most this many
# field names, each clipped to this width, the rest folded into "+N more".
PARAMS_MAX_FIELDS = 3
PARAMS_FIELD_WIDTH = 24


def compact_params(record: dict) -> str:
    """The one-line params rendering history rows and the sidebar share:
    model, pages, tier for parse; schema field summary + model for
    extract. Bounded whatever the inputs: the field summary is capped, so
    neither the table's column budget nor a plain line can blow up."""
    params = record.get("params") or {}
    if record["kind"] == "parse":
        parts = [params.get("model") or "?"]
        pages = (params.get("options") or {}).get("pages")
        if pages:
            parts.append("pages " + ",".join(map(str, pages)))
        if params.get("tier"):
            parts.append(params["tier"])
        return " · ".join(parts)
    fields = record.get("fields") or []
    shown = [
        name
        if len(name) <= PARAMS_FIELD_WIDTH
        else name[: PARAMS_FIELD_WIDTH - 1] + "…"
        for name in fields[:PARAMS_MAX_FIELDS]
    ]
    summary = ", ".join(shown) or "?"
    if len(fields) > PARAMS_MAX_FIELDS:
        summary += f" +{len(fields) - PARAMS_MAX_FIELDS} more"
    parts = [summary]
    if params.get("model"):
        parts.append(params["model"])
    return " · ".join(parts)


def source_name(source: str | None) -> str | None:
    """How listings and the sidebar name a source: the file name for paths,
    the whole string for URLs (Path() would mangle them)."""
    if source is None:
        return None
    if source.startswith(("http://", "https://")):
        return source
    return Path(source).name
