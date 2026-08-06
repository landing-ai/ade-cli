---
name: ade
description: Parse documents and extract schema-shaped data with the ADE (Agentic Document Extraction) v2 APIs through the ade CLI. A local job-item store makes every run idempotent, resumable, and citable — repeat runs are free, interrupted runs resume, and every answer can cite element ids with visual evidence.
---

# ade — agent skill

ade drives the ADE (Agentic Document Extraction) v2 document APIs
(parse, extract) and persists
everything it produces in a local store under `~/.ade`. You are the
judgment; the CLI is deterministic plumbing: it parses, persists,
searches, renders, and totals — it never ranks or selects for you.

## First move

```
ade help --json
```

One call returns the whole shipped surface: every command and flag, each
verb's `--json` result shape, the exit states, and the store layout.
Prefer it over N `--help` round trips. `ade help workflow` (also
`output`, `credentials`, `errors`) is the conceptual page behind it.
Check credentials with `ade auth status --json`; if logged out, set
`ADE_API_KEY`, or pipe a key in (`echo $KEY | ade auth login`) — no
terminal required either way.

## Conventions

- **Always pass `--json`.** Every command emits one stable JSON
  object/array on stdout — errors and pending payloads included. Human
  text is for terminals, not for parsing. The **whole result is on
  stdout**: the extraction, the matches, the paths. Reading the store is
  a convenience, never a requirement — and `ade help --json` publishes
  each verb's shape, so you never have to discover it by running.
- **`--id-only` when you only need the id.** `parse`, `extract`, and
  `find` take it: the id(s), one per line, nothing else —
  `JOB=$(ade parse -d report.pdf --id-only)`. Errors go to stderr, so a
  captured id is never a sentence.
- **Job item ids are the currency.** Every parse or extract run is a
  *job item*: one folder under `~/.ade/jobs/<job-item-id>/`, keyed by the
  invocation (verb × environment × source × content × params; URL
  sources are keyed by the URL alone — no content component, so remote
  drift does not re-key). Commands accept an
  unambiguous id prefix. Changing the file, its path, or any param mints
  a *sibling* item — nothing is silently replaced.
- **Commands are guarantees.** `parse` means "ensure this exact run
  exists", not "fire a request". Re-running an already-done invocation is
  **free** — served from disk with an `already parsed` notice and
  `"cached": true`. Only `--force` re-bills.

## The loop

1. **Parse** (free if already done):

   ```
   ade parse -d report.pdf --json
   ```

   The payload carries `job_item_id` — hold onto it. Everything else
   keys off it. The markdown and the elements projection stay on disk
   unless you ask for them: `--include markdown`, `--include elements`.

2. **Discover element ids** (local, instant, no API call):

   ```
   ade find JOB_ITEM_ID "total revenue" --json
   ade find JOB_ITEM_ID --type table_cell --page 3 --json
   ```

   (`--job JOB_ITEM_ID` is the equivalent flag spelling — repeat it to search
   several items in one call.)

   Matches are citation records: `{job_item_id, element_id, type, page,
   box, text}`. Filters compose (AND); results come in document order,
   never ranked — selection is your job.

3. **Extract** structured data against a JSON Schema:

   ```
   ade extract JOB_ITEM_ID --schema schema.json --json
   ```

   The result is its own job item. The payload's `extraction` key is the
   schema-shaped result itself — read it there, not off disk. Per-field
   evidence (`evidence`, persisted as `evidence.json`) joins extraction
   spans to element ids, pages, and boxes — non-empty fields the model
   synthesised rather than quoted are flagged `ungroundable`;
   empty-valued fields (blank cells, absent optionals — nothing to
   ground) are labeled `empty`. Neither is ever silently dropped.

4. **Cite and show.** `ade view JOB_ITEM_ID --json` builds a
   self-contained HTML viewer; deep links are the citation contract:

   ```
   ade view JOB_ITEM_ID --element-id ELEMENT_ID --json   # emits view.html#element=ELEMENT_ID
   ade crop JOB_ITEM_ID --element-id ELEMENT_ID --json   # PNG of that element's region
   ade crop JOB_ITEM_ID --type figure --json             # every figure, one command
   ```

   End answers with one deep link per job item, citing element ids.
   Use `crop` when you need to *look at* evidence mid-reasoning — it
   takes `find`'s own filters (`--type`, `--page`, `--all`), so a
   selection crops in one call and returns `crops[]`; never loop `find`
   into `crop` yourself.

## Reuse posture — parse bills once

Every parse the CLI ever runs is a reusable job item. Given a document
path, `extract -d FILE --schema …` reuses the latest completed parse of
that path+content (logged in the summary; no parse billed). If none
exists, it runs a **standalone parse first** — a normal, top-level
parse item, exactly as if you had run `parse -d` — then the extraction
referencing it: two billable runs, both itemised. Repeat `extract -d`
runs of the same file then reuse that parse, so it bills exactly once.

Prefer the explicit two-step (`parse -d`, then `extract JOB_ITEM_ID`) when
you want the same parse to feed several schemas — the id makes the reuse
visible.

## Pending and resume

Wait expiry is a normal outcome, not an error. If the poll budget
(`--wait`, default 600s) runs out, the command exits with code 3 and a
`{"status": "pending", "run_id": …, "job_item_id": …}` payload while the
run continues server-side (submitted work always completes and bills —
there is no cancel). **The recovery gesture is always the same command,
re-run.** A re-run joins the recorded run; it never resubmits and never
re-bills. Interrupts (Ctrl-C) stop the waiting, not the work — same
gesture. `--wait 0` submits and returns immediately.

Submit-and-return, then collect later — the pending payload carries the
id too, so this works in both steps:

```
JOB=$(ade parse -d report.pdf --wait 0 --id-only)   # exit 3, run continuing
ade parse -d report.pdf --json                       # re-run: resumes, never re-bills
```

## Exit codes

| Code | State | Meaning |
|------|-------|---------|
| 0 | ok | Success — the payload is on stdout. |
| 1 | failed | The run failed or the target cannot serve the request. |
| 2 | usage | The invocation itself was wrong; nothing was submitted. |
| 3 | pending | Wait budget expired; the run continues server-side. Re-run the same command to resume. |
| 4 | rate_limited | Submit was rate-limited and the wait budget ran out before a run existed; nothing billed. Re-run to retry. |

## Reading the store directly

Optional, never required: every result is already on stdout. Artifacts
are plain files — `history list --json` gives every item's records, and
the summaries print each item's store path.

- `parse.json` — raw ParseResponse (ground truth, verbatim)
- `parse.md` — the parse markdown extraction spans index
- `elements.json` — flat element records with inline grounding; boxes
  are normalized `{xmin, ymin, xmax, ymax}` fractions of page size in
  `[0, 1]`
- `extract.json` — raw extraction result with per-field spans
- `evidence.json` — the field→box join (element ids, pages, boxes)

One vocabulary note when reading these files: on-disk records
(`meta.json`, `job.json`, `parse/ref.json`) spell the server-side run id
as `job_id` — the wire contract's name for the same value `--json`
payloads report as `run_id`. Neither is ever the job item id.

Prefer `find` over loading `elements.json` into context: it returns
joined records, not lines. `history clear JOB_ITEM_ID` deletes an item;
clearing a parse item cascades to the extractions referencing it.

## Sharp edges

- **`--options` is a verbatim ParseOptions pass-through** — the server
  rejects unknown keys with a 422, so consult `ade help parse` for
  the accepted keys rather than guessing.
- **Parse variants coexist.** The same document parsed with different
  params is a sibling item, not a replacement. An extraction goes stale
  only when its exact parse item is re-run in place with `--force`.
- **Moving or editing a file changes identity** — the next parse of it
  is a new job item that bills. The old item and its evidence stay
  intact and true of the run they came from.
- **`--markdown` extractions have no page evidence** (there is no parse
  to join against) — evidence degrades to spans-only, and `view`
  renders the markdown pane alone.
- **URL parses have no local bytes**, so page imagery renders from an
  attached copy: `parse --document-url … --keep-copy` fetches it at
  parse time (reliable — pre-signed URLs expire); otherwise the first
  `view`/`crop` downloads it automatically (`--no-download` skips;
  the payload records `downloaded`, and on a failed fetch `view`
  degrades to an empty preview with `download_error` while `crop`
  errors). Markdown, elements, and extractions work either way.
