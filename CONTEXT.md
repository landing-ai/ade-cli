# ade-cli

A CLI over the ADE (Agentic Document Extraction) v2 document APIs (parse, extract) that persists everything it produces in a local store and serves every read from it. Agents are the primary callers; humans get one explorable artifact per job item.

## Language

### Runs and identity

**Document**:
A source file or URL fed to a run. It is one component of identity, not the whole of it — the same document processed differently yields different job items.
_Avoid_: file (ambiguous with store artifacts)

**Job item**:
One parse or extract invocation and everything it produced, the store's unit. Its identity is the invocation: verb × environment × source path × document bytes × params (URL sources: verb × environment × URL × params — the CLI never sees the bytes) — any component changing mints a sibling item; one environment's result never serves another's request.
_Avoid_: doc (retired), run (informal)

**Job item id**:
The CLI's local primary key for a job item, derived from the invocation identity. Commands take it (or an unambiguous prefix) — it is distinct from the server's `job_id` and the parse-trailer `doc_id`.
_Avoid_: doc id (retired), REF (retired — paths are accepted only by the convenience verbs)

**Variant**:
A sibling job item for the same document with different params. Variants coexist in history; nothing is silently replaced.
_Avoid_: live parse (retired — "last parse wins" is gone)

### Work and guarantees

**Guarantee command**:
A command that ensures a state rather than fires an action — idempotent, resumable, interrupt-safe. `parse` means "ensure parsed"; `extract` means "ensure extracted".
_Avoid_: action, request (for these commands)

**Job**:
The server-side unit of async work, minted at submit and polled by id. No cancel exists; a submitted job always completes and bills.

**Claim ticket**:
The local record that a job is in flight for a job item, written atomically before submit. Re-running the same command resumes the ticket's job instead of resubmitting.
_Avoid_: lock, lease

**Wait**:
The poll-phase clock of a guarantee command. Wait expiry leaves the job running and is a normal pending outcome, not an error.
_Avoid_: timeout (reserved for internal transport guards)

**Service tier**:
The async execution lane: `priority` (full price, fast lane) or `standard` (half price, slower lane).

**Stale**:
An extraction whose referenced parse was replaced in place by a forced re-parse — the one remaining cause. Its spans index markdown that no longer exists; it is kept and badged, never silently served. Variants never stale anything.

**Expired**:
A pending job whose server retention has passed (polls 404). Treated as absent: the next run resubmits fresh.

**Unreadable**:
A job that completed server-side but whose poll payload carried no inline result this CLI can read (URL delivery, or an unsupported response contract). Recorded on the claim ticket; re-running re-polls the same job — never resubmits, since the completed job already billed.

### Parse results

**Element**:
The API's unit of document content: an id, a type (text, table, table_cell, figure, …), a span, and grounding. Element ids are the citation currency.
_Avoid_: block, node, chunk

**Span**:
`[start, end)` Unicode code point offsets into a parse's markdown (the wire calls it `range`). Spans are only meaningful against the exact markdown they index.

**Grounding**:
The spatial side of a parse, carried inline on every structure-tree node: `{page, range, box}`, plus finer `atomic_grounding` segments (visual lines today) on leaf elements.
_Avoid_: layout, coordinates, parts

**Box**:
A bounding box `{xmin, ymin, xmax, ymax}` in normalized page coordinates — `[0, 1]` fractions of page width/height; multiply by a raster's dimensions to get pixels.

### Extraction results

**Extraction**:
A schema-driven structured read of one parse's markdown. Always its own job item, which either references the parse job it ran against (running one first if none exists — every parse is a reusable job item) or carries bring-your-own markdown.
_Avoid_: embedded parse (retired — a parse triggered by extract is a normal, reusable parse job)

**Evidence**:
The local field→boxes join: extraction spans resolved through elements to pages and boxes, computed offline from stored artifacts.

**Ungroundable**:
An extraction field with a *non-empty* value the model synthesised rather than quoted (null spans). It has no evidence and is surfaced as such, never silently dropped — it is the case that deserves attention.
_Avoid_: hallucinated (the value may be correct — it just isn't quoted)

**Empty field**:
An extraction field with no value at all (null or `""` — a blank cell, an absent optional). It has null spans too, but there is no box an empty string could ground to, so it is labeled `empty` and reads quietly. Distinct from ungroundable, which is a value that *should* have evidence.

### Auth

**Target environment**:
The environment one invocation runs against, resolved fresh every command
(ADR-0003): `--env` flag → `ADE_ENV` variable → production. Nothing is
stored about the choice — there is no "current" environment — and
credentials are stored and read per environment. The environment is part
of job-item identity, and an extract over a parse item inherits the
item's environment (its server-side parse job exists nowhere else).
_Avoid_: active environment, current env, switching (nothing persists to switch)

**Login verification**:
The live check an API-key login runs before storing anything (ADR-0007):
one empty-batch `POST /v2/telemetry` against the resolved target with
the candidate key as Bearer. Only the target's 401 is authoritative
about the key (one canonical invalid-key message, never the platform's
own 401 body — those vary by which check rejected the key); any other
failure is reported as a platform problem, and nothing is stored either
way. OAuth logins verify themselves through the token exchange.
_Avoid_: validation against a dedicated auth route (none exists in the v2 contract)

**Provider**:
The per-environment Logto coordinates the browser login runs against: issuer (`login.*`, never the `logto.*` admin portal), client id, and the RFC 8707 resource indicator (the token audience — the environment's API endpoint). Defaults are data; `config.json`'s `oauth.<environment>` block overrides field by field. Browser login is a public login method (ADR-0008; ADR-0004's launch gate is deleted now that the platform accepts OIDC tokens).

**OAuth session**:
The refreshable browser-login credential for one environment: access + refresh tokens, identity, expiry. Access tokens refresh silently near expiry or once after a 401; refresh tokens rotate on every use, so refresh holds a cross-process lock and re-reads before spending one.
_Avoid_: token (alone — access and refresh tokens have different lifecycles)

### Store

**Store**:
`~/.ade` — the local source of truth for documents. Raw API responses are ground truth and never edited; every index is a recomputable projection of them.

**Raw artifact**:
A verbatim persisted API response. Everything else can be rebuilt from raw artifacts plus the source document.

**Derived index**:
A recomputable projection over raw artifacts (the flat element list, evidence, the viewer), kept because it is read repeatedly.
