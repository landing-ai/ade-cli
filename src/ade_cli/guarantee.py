"""The shared guarantee lifecycle: ensure a server-side job's result.

A guarantee command ensures a state rather than fires an action. Both
network verbs (``parse``, ``extract``) run this machinery: a claim
ticket written atomically before submit (of any number of racing
processes, exactly one submits; the rest join as pollers), submit 429s
retried with Retry-After inside the ``--wait`` budget, transient poll
5xx ridden out like pending ticks inside that same budget, wait expiry
and Ctrl-C as normal pending outcomes (the job keeps running server-side),
failed and expired (poll-404) jobs resubmitted fresh, completed jobs
whose result this CLI cannot read — missing inline, or a schema its
contract does not cover — marked ``unreadable`` (a re-run re-polls the
same job, never resubmits — the job billed once; only the ``fresh``
consent abandons it), and completion published under the item lock only
while the ticket still owns its slot.

What differs per verb — the submit POST, the poll route, the cache key,
and the artifacts written on completion — is injected by the caller.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterator, NoReturn

import httpx
import typer

from . import update
from .config import DEFAULT_ENVIRONMENT
from .gateway import GatewayError
from .oauth import ReloginRequired
from .output import (
    EXIT_FAILED,
    EXIT_PENDING,
    EXIT_RATE_LIMITED,
    exit_with,
)
from .ports import Clock
from .store import JobStore


class Tier(str, Enum):
    """The async execution lane: priority (full price, fast lane) or
    standard (half price, slower lane). Same posture on both verbs."""

    priority = "priority"
    standard = "standard"

POLL_START_SECONDS = 1.0
POLL_FACTOR = 1.5
POLL_CAP_SECONDS = 10.0

# A submitless ticket younger than this may belong to a process still inside
# submit; older means its owner crashed between claim and submit. Owners keep
# the lease alive by rewriting submitted_at: around 429 retry sleeps, and via
# a heartbeat thread while the submit POST itself is in flight — so a live
# owner's ticket can never age past the grace, however long an upload takes.
RECLAIM_GRACE_SECONDS = 600.0
LEASE_RENEW_SECONDS = RECLAIM_GRACE_SECONDS / 4


# Redraw cadence of the tty line while waiting out a poll backoff: fast
# enough to read as alive, slow enough to cost nothing.
ANIMATE_TICK_SECONDS = 0.5
ANIMATE_DOTS = 3  # dot cycle: . .. ... . ..

# What the progress line calls each verb's work while it runs (#69). The
# phase verb *replaces* the raw server status ("processing") rather than
# prefixing it — "parsing… processing" said the same thing twice (field
# report on #46) — because the command name alone can't disambiguate:
# `extract -d` on a never-parsed document runs two jobs back-to-back
# (parse-first, then extract), and bare statuses rendered both phases as
# identical processing/completed lines.
PHASE_VERBS = {"parse": "parsing", "extract": "extracting"}


class Progress:
    """Job progress rendered to stderr while polling (#33); stdout stays
    byte-identical in every mode — it carries only the command's payload.

    ``tty``: one live-rewriting status line (phase + percent + elapsed),
    with milestone transitions (parse submitted → parsing → parse
    completed) updating the same line, and cycling dots + a ticking elapsed
    while waiting between polls (the backoff grows to 10s — a static line
    reads as hung).
    ``plain`` (piped stderr): no rewriting — one line per milestone and one
    per 10%-decade of progress (the first observation in a decade prints,
    later ones in the same decade dedup), so logs stay readable. ``off``
    (``--json``): fully silent.
    """

    def __init__(self, clock: Clock, mode: str) -> None:
        self.clock = clock
        self.mode = mode  # "tty" | "plain" | "off"
        self._started: float | None = None
        self._live = False  # a tty line is on screen, not yet newline-terminated
        self._emitted: tuple | None = None  # plain-mode dedup key
        self._detail: str = ""  # what the line reports (phase word, plus pct when known)
        self._phase = 0  # dot-animation phase, advanced per sleep tick

    def _line(self, dots: int = 0) -> str:
        # The observation, phase-named: "parsing.. · 26s", "extracting
        # 45% · 30s" (see PHASE_VERBS for why the phase verb stands in for
        # the raw server status instead of prefixing it).
        elapsed = int(self.clock.monotonic() - (self._started or 0))
        return f"{self._detail}{'.' * dots} · {elapsed}s"

    def _redraw(self, dots: int = 0) -> None:
        # Rewrite in place: return to column 0, write, erase any tail
        # left over from a longer previous line.
        typer.echo(f"\r{self._line(dots)}\x1b[K", nl=False, err=True)
        self._live = True

    def update(self, *, label: str | None = None, fraction: float | None = None) -> None:
        """Render one poll observation: a milestone ``label`` ("parse
        submitted", "parsing", "extract completed"), optionally carrying a
        0-1 ``fraction`` from the server's best-effort per-page
        ``progress`` field ("parsing 45%")."""
        if self.mode == "off":
            return
        if self._started is None:
            self._started = self.clock.monotonic()
        pct = None if fraction is None else max(0, min(100, round(fraction * 100)))
        parts = [p for p in (label, None if pct is None else f"{pct}%") if p]
        self._detail = " ".join(parts)
        self._phase = 0  # fresh observation: the dot cycle starts over
        if self.mode == "tty":
            self._redraw()
            return
        coarse = None if pct is None else pct - pct % 10
        if (label, coarse) == self._emitted:
            return
        self._emitted = (label, coarse)
        typer.echo(self._line(), err=True)

    def sleep(self, seconds: float) -> None:
        """Wait out a poll backoff. On a live tty line, sleep in short ticks
        and redraw with cycling dots and a ticking elapsed; everywhere else
        one plain sleep — piped logs and tests see the exact same sleep
        pattern as before."""
        if self.mode != "tty" or not self._live:
            self.clock.sleep(seconds)
            return
        remaining = seconds
        while remaining > 0:
            tick = min(ANIMATE_TICK_SECONDS, remaining)
            self.clock.sleep(tick)
            remaining -= tick
            self._phase += 1
            self._redraw(dots=(self._phase - 1) % ANIMATE_DOTS + 1)

    def close(self) -> None:
        """Terminate a live tty line so whatever prints next (the stdout
        summary, an error, the shell prompt) starts on its own line.
        Idempotent; a no-op unless a live line is pending."""
        if self._live:
            typer.echo("", err=True)
            self._live = False


@dataclass
class Outcome:
    """A guarantee that reached ``completed`` with an inline result."""

    result: dict
    job_id: str
    ticket: dict


def failed_outstanding(store: JobStore, item_id: str, ticket_name: str, params: dict) -> bool:
    """True when the slot's last attempt for these exact params failed: the
    recovery gesture for a reported failure is a fresh resubmit, never a
    silent cache hit."""
    ticket = store.read_json(item_id, ticket_name)
    return (
        ticket is not None
        and ticket.get("state") in ("failed", "cancelled")
        and ticket.get("params") == params
    )


def summary_note(*, cached: bool, stored: bool) -> str:
    """The provenance suffix both verbs' summaries carry."""
    if cached:
        return " (served from store, no API call)"
    if not stored:
        return " (a newer guarantee owns the store; artifacts not saved)"
    return ""


def schema_problem(result: object, err: Exception) -> str:
    """One line naming how a completed result failed to read under this
    CLI's contract, plus the shape actually observed — evidence for a
    bug report ("I'm on the latest CLI and still see this"), never a
    guessed cause."""
    # str() each key: the diagnosis must never itself raise on a shape
    # weirder than expected (non-string or mixed-type keys).
    keys = (
        ", ".join(sorted(str(key) for key in result))
        if isinstance(result, dict)
        else type(result).__name__
    )
    return f"{type(err).__name__}: {err} (result keys: {keys})"


@dataclass
class Guarantee:
    """One verb's run of the shared lifecycle against one ticket slot."""

    store: JobStore
    item_id: str
    kind: str  # the verb this item runs ("parse" | "extract"), recorded on the ticket
    ticket_name: str  # the claim slot under the item dir (both verbs: "job.json")
    params: dict  # the cache/join key recorded on the ticket
    tier: str
    source: str | None  # provenance for read models pre-completion
    wait: float
    clock: Clock
    as_json: bool
    context: dict  # ids merged into every machine payload (doc_id, extract_id, …)
    noun: str  # human name for failure lines ("Parse", "Extract")
    endpoint_label: str  # how messages name the API target (environment or raw URL)
    # The resolved environment name (ResolvedConfig.environment: --env /
    # ADE_ENV / production — the name credentials file under, even when
    # ADE_ENDPOINT overrides the URL), recorded on the ticket so read
    # models can say which API target a run addressed.
    environment: str
    post: Callable[[dict], httpx.Response]  # ticket -> submit POST (202 + job_id)
    poll: Callable[[str], httpx.Response]  # job_id -> poll GET
    interrupted_no_job_hint: str  # human recovery line when no job was recorded
    # The --force consent: abandon a joinable unreadable ticket and submit
    # fresh. Off by default — an unreadable job billed once, and only an
    # explicit user gesture may bill another.
    fresh: bool = False
    # Whether stderr is a terminal — picks the progress rendering mode
    # (#33); --json silences progress entirely, whatever the stream is.
    stderr_tty: bool = False
    progress: Progress = field(init=False)

    def __post_init__(self) -> None:
        mode = "off" if self.as_json else ("tty" if self.stderr_tty else "plain")
        self.progress = Progress(self.clock, mode)

    def _item_label(self) -> str:
        """The job item id as a human-line suffix (#167): every message
        that names a run must also name the local key the next command
        (view, extract, history clear, a resumed run) actually takes —
        the --json payloads always carried it via ``context``, but the
        human lines used to surface only the run id."""
        item_id = self.context.get("job_item_id")
        return f" (job item {item_id})" if item_id else ""

    def ensure(self) -> Outcome:
        """Run claim → submit → poll to a completed job with an inline
        result; every other outcome exits with its machine payload."""
        job_id: str | None = None

        def exit_pending() -> NoReturn:
            self.progress.close()
            if job_id:
                human = (
                    f"Run {job_id}{self._item_label()} continues "
                    "server-side; re-run the same command to resume "
                    "waiting (it will never resubmit)."
                )
            else:
                human = self.interrupted_no_job_hint
            exit_with(
                # "run_id" is the user-facing name of the server-side id
                # (the wire and the claim ticket still spell it job_id).
                {"status": "pending", "run_id": job_id, **self.context},
                human,
                as_json=self.as_json,
                code=EXIT_PENDING,
            )

        try:
            job_id, ticket = self._acquire_job()
            self.progress.update(label=f"{self.kind} submitted")

            if self.wait <= 0:
                exit_pending()  # submit-and-return; the claim ticket is saved

            poll_deadline = self.clock.monotonic() + self.wait
            delay = POLL_START_SECONDS
            resubmitted = False
            while True:
                if self.clock.monotonic() >= poll_deadline:
                    exit_pending()  # never poll past the promised budget
                try:
                    polled = self.poll(job_id)
                except GatewayError as error:
                    if error.status_code == 404 and not resubmitted:
                        # Expired: server retention passed, the job is gone
                        # (poll 404s). Treated as absent — steal only the
                        # exact ticket whose job 404ed (a fresh ticket someone
                        # else already claimed stays live) and re-acquire:
                        # resubmit or join.
                        resubmitted = True
                        self.store.cas(self.item_id, self.ticket_name, ticket, None)
                        job_id, ticket = self._acquire_job()
                        # The fresh job gets a fresh poll budget — re-anchored,
                        # same as after any submit.
                        poll_deadline = self.clock.monotonic() + self.wait
                        delay = POLL_START_SECONDS
                        continue
                    if error.status_code >= 500:
                        # Transient server failure on the poll route: the job
                        # is unknown, not gone — a single 5xx must not kill a
                        # run whose job may already be completed (#19). Ride
                        # it out like a pending tick, inside the same budget;
                        # a persistently down server exits pending with the
                        # ticket intact. Retry-After (e.g. on 503) can
                        # lengthen a tick but never shrink it below the
                        # backoff — the header is a minimum, and a 5xx storm
                        # with Retry-After: 0 must not turn the poll into a
                        # zero-sleep spin. The submit POST is never retried
                        # this way — without an idempotency key it can
                        # double-bill.
                        retry_in = max(delay, error.retry_after or 0.0)
                        if self.clock.monotonic() + retry_in > poll_deadline:
                            exit_pending()
                        self.progress.sleep(retry_in)
                        delay = min(delay * POLL_FACTOR, POLL_CAP_SECONDS)
                        continue
                    self._exit_http_error(error)
                payload = polled.json()
                status = payload["status"]
                if status in ("pending", "processing"):
                    # progress is 0-1, per-page, best-effort — guaranteed on
                    # every processing poll by the current gateway. The phase
                    # verb ("parsing"/"extracting") stands in for the raw
                    # status word (#69, see PHASE_VERBS); "pending" keeps its
                    # own word — the job is queued, not being worked — but
                    # phase-qualified ("parse pending"). The fraction rides
                    # beside the verb; dropped when absent or non-numeric
                    # (bool excluded: a JSON true would read as 100%), and at
                    # 0.0 too: that's the gateway's nothing-to-report floor
                    # (child not started, query failed, or a 1-page doc with
                    # no per-page milestone to cross) — "0%" would dress the
                    # floor up as a measurement.
                    verb = (
                        PHASE_VERBS.get(self.kind, self.kind)
                        if status == "processing"
                        else f"{self.kind} {status}"
                    )
                    fraction = payload.get("progress")
                    if (
                        isinstance(fraction, (int, float))
                        and not isinstance(fraction, bool)
                        and fraction > 0
                    ):
                        self.progress.update(label=verb, fraction=fraction)
                    else:
                        self.progress.update(label=verb)
                    if self.clock.monotonic() + delay > poll_deadline:
                        exit_pending()
                    self.progress.sleep(delay)
                    delay = min(delay * POLL_FACTOR, POLL_CAP_SECONDS)
                    continue
                if status in ("failed", "cancelled"):
                    self.progress.close()
                    # CAS: a delayed poller must not overwrite a fresh claim
                    # someone else installed after this job expired for them.
                    self._update_ticket(ticket, {**ticket, "state": status})
                    error = payload.get("error") or {}
                    reason = error.get("message") or error.get("code") or status
                    exit_with(
                        {"status": status, "run_id": job_id, **self.context, "reason": reason},
                        f"{self.noun} {status}: {reason} "
                        f"(run {job_id}{self._item_label()}).",
                        as_json=self.as_json,
                        code=EXIT_FAILED,
                    )
                if status == "completed":
                    # Phase-qualified so the two back-to-back jobs of a fresh
                    # `extract -d` never read as two bare "completed" (#69).
                    self.progress.update(label=f"{self.kind} completed")
                    self.progress.close()
                    break
                self.progress.close()
                exit_with(  # a status this CLI doesn't know is never silent success
                    {"error": "unexpected_status", "status": status, "run_id": job_id, **self.context},
                    f"Unexpected run status {status!r} "
                    f"(run {job_id}{self._item_label()}); upgrade ade "
                    "(re-run the installer, or `uv tool upgrade ade-cli`) and retry.",
                    as_json=self.as_json,
                    code=EXIT_FAILED,
                )
        except KeyboardInterrupt:
            # Ctrl-C stops the waiting, never the work (no server cancel
            # exists; a submitted job bills regardless). Same command is the
            # recovery.
            exit_pending()

        result = payload.get("result")
        if result is None:
            # Completed, but nothing inline this CLI can read. Diagnose from
            # the payload actually observed instead of asserting one cause.
            # The ticket is marked unreadable — its own state, not failed:
            # failed tickets resubmit fresh (failed_outstanding), and a
            # resubmit here would re-bill a job that reads the same way.
            output_url = payload.get("output_url")
            if output_url:
                human = (
                    f"Run {job_id}{self._item_label()} completed without "
                    f"an inline result; the response carries "
                    f"output_url={output_url}, which ade does not fetch."
                )
            elif "data" in payload:
                # The pre-cutover envelope: updating ade would not help —
                # an up-to-date CLI is exactly what fails here. The endpoint
                # simply hasn't promoted to the API release this CLI speaks.
                human = (
                    f"Run {job_id}{self._item_label()} completed, but "
                    f"{self.endpoint_label} "
                    "answered with the pre-cutover response contract "
                    "(top-level 'data' instead of 'result'). This CLI is "
                    "current; that API target has not promoted to the new "
                    "release yet. Re-run once it promotes, or point the CLI "
                    "at an already-promoted environment "
                    "(`ade auth login --env <name>`)."
                )
            else:
                human = (
                    f"Run {job_id}{self._item_label()} completed without "
                    f"an inline result; the response's top-level keys "
                    f"were: {', '.join(sorted(payload))}."
                )
            # The diagnosis rides on the ticket: job.json is the durable
            # record history list reads the reason from.
            self._update_ticket(ticket, {**ticket, "state": "unreadable", "reason": human})
            exit_with(
                {
                    "error": "missing_result",
                    "run_id": job_id,
                    **self.context,
                    "output_url": output_url,
                    "payload_keys": sorted(payload),
                },
                human,
                as_json=self.as_json,
                code=EXIT_FAILED,
            )
        return Outcome(result=result, job_id=job_id, ticket=ticket)

    def publish(self, outcome: Outcome, write: Callable[[], None]) -> bool:
        """Publish the live artifact set under the item lock so two completing
        pollers can never interleave their writes into a mixed set — and only
        while this poller's ticket still owns the slot: a job superseded by a
        newer guarantee completes without touching the store."""
        with self.store.lock(self.item_id):
            stored = self.store.read_json(self.item_id, self.ticket_name) == outcome.ticket
            if stored:
                write()
        # CAS: a delayed poller finishing an expired-and-replaced job must not
        # overwrite the fresh claim that superseded it.
        self._update_ticket(outcome.ticket, {**outcome.ticket, "state": "completed"})
        return stored

    def unreadable_result(self, outcome: Outcome, problem: str) -> NoReturn:
        """Reject a completed result the verb cannot consume — called before
        any artifact write, so a schema this CLI does not cover fails whole,
        never as a torn artifact set or a raw traceback.

        The ticket is marked ``unreadable`` with the reason (job.json is the
        durable record ``history`` reads), not ``failed``: failed resubmits
        fresh and re-bills. The advice defaults to "upgrade ade" — the
        CLI usually lags the API, so an unrecognized shape is almost always
        newer than this binary; when that guess is wrong, the re-run costs a
        re-poll, never a bill, and --force stays the consented way out.
        """
        self._update_ticket(
            outcome.ticket, {**outcome.ticket, "state": "unreadable", "reason": problem}
        )
        exit_with(
            {
                "error": "unsupported_result_schema",
                "run_id": outcome.job_id,
                **self.context,
                "reason": problem,
            },
            f"Run {outcome.job_id}{self._item_label()} completed, but "
            "its result does not match "
            f"the contract this CLI understands: {problem}. The API "
            "usually runs ahead of the CLI — upgrade ade (re-run the "
            "installer, or `uv tool upgrade ade-cli`), then re-run the "
            "same command (it re-reads this job without re-billing); "
            "--force abandons the job and submits a fresh one (bills again).",
            as_json=self.as_json,
            code=EXIT_FAILED,
        )

    def _exit_http_error(self, error: GatewayError) -> NoReturn:
        self.progress.close()
        # The server's machine code (e.g. validation_error on a rejected
        # options key) rides along in both renderings, so a 422's cause is
        # named, never a bare status line.
        label = f"{error.code}: " if error.code else ""
        human = f"HTTP {error.status_code}: {label}{error.detail}"
        # A registry-style "unknown model" rejection usually means the API
        # moved past this CLI build (#138) — say so, in both renderings.
        hint = update.unknown_model_hint(error.detail)
        if hint:
            human = f"{human} {hint}"
        if error.status_code == 401 and not isinstance(error, ReloginRequired):
            # One canonical line for every rejected credential (#117): the
            # platform's 401 bodies vary by which check rejected the key
            # ("Invalid API Key Format" vs "Invalid API Key, please…"), so
            # the human line never quotes them — the server text stays in
            # the machine payload's ``message``. ReloginRequired keeps its
            # own detail: it already names the OAuth cause and remediation.
            login_hint = (
                f"ade auth login --env {self.environment}"
                if self.environment != DEFAULT_ENVIRONMENT
                else "ade auth login"
            )
            human = (
                f"HTTP 401: {self.endpoint_label} rejected the credential — "
                "the API key is invalid or revoked. Run "
                f"`{login_hint}` to re-authenticate (ADE_API_KEY, if set, "
                "overrides stored credentials)."
            )
        exit_with(
            {
                "error": "http",
                "status_code": error.status_code,
                "code": error.code,
                "message": error.detail,
                **({"hint": hint} if hint else {}),
                **self.context,
            },
            human,
            as_json=self.as_json,
            code=EXIT_FAILED,
        )

    def _renew_lease(self, ticket: dict) -> bool:
        """CAS-renew the claim's lease. False means the lease was
        legitimately reclaimed by another owner — the caller must stop
        acting as the owner, and must never overwrite the new claim."""
        renewed = {**ticket, "submitted_at": self.clock.now()}
        if self.store.cas(self.item_id, self.ticket_name, ticket, renewed):
            ticket.update(renewed)
            return True
        return False

    @contextmanager
    def _lease_heartbeat(self, ticket: dict) -> Iterator[None]:
        """Keep the claim's lease renewed while the submit POST is in flight.

        httpx timeouts bound individual socket operations, not the whole
        request, so a large upload can legitimately run past any fixed bound;
        the heartbeat (real-time paced — it must beat while the main thread
        is blocked in the POST) keeps waiters from mistaking us for crashed.
        A failed renewal stops the heartbeat: the job_id CAS after the POST
        then no-ops, leaving the new owner's claim untouched.
        """
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(LEASE_RENEW_SECONDS):
                if not self._renew_lease(ticket):
                    return

        beater = threading.Thread(target=beat, daemon=True)
        beater.start()
        try:
            yield
        finally:
            stop.set()
            beater.join(timeout=1.0)

    def _submit(self, ticket: dict) -> str:
        """Submit, honoring 429 Retry-After inside a --wait budget anchored
        at this call's first attempt (recovery work that preceded it — grace
        waits, expired polls — must not eat the retry budget). The caller
        owns ``ticket`` (the claim); its lease stays renewed throughout."""
        submit_deadline = self.clock.monotonic() + self.wait
        while True:
            try:
                with self._lease_heartbeat(ticket):
                    submitted = self.post(ticket)
            except GatewayError as error:
                if error.status_code != 429:
                    if 400 <= error.status_code < 500:
                        # Deterministic rejection: the server answered and no
                        # job was minted. Release our claim so a corrected
                        # invocation submits immediately instead of waiting
                        # out the lease grace. 5xx keeps the lease — the
                        # outcome is ambiguous, and the grace ages it out.
                        self.store.cas(self.item_id, self.ticket_name, ticket, None)
                    self._exit_http_error(error)
                retry_after = (
                    POLL_START_SECONDS
                    if error.retry_after is None  # 0 is valid: retry now
                    else error.retry_after
                )
                if self.clock.monotonic() + retry_after >= submit_deadline:
                    # Budget dies before a job exists (>= so a zero budget
                    # with Retry-After: 0 can never spin): nothing was
                    # submitted, so our claim — and only ours — is released.
                    self.store.cas(self.item_id, self.ticket_name, ticket, None)
                    self.progress.close()
                    exit_with(
                        {
                            "status": "rate_limited",
                            **self.context,
                            "retry_after": retry_after,
                            "message": error.detail,
                        },
                        f"Rate limited before submit ({error.detail}); "
                        f"retry after {retry_after:g}s.",
                        as_json=self.as_json,
                        code=EXIT_RATE_LIMITED,
                    )
                # Sleep in lease-renewing chunks: a long Retry-After must not
                # age the claim past the reclaim grace.
                remaining = retry_after
                while True:
                    if not self._renew_lease(ticket):
                        # Fenced: the lease was reclaimed while we waited.
                        # The new owner submits; we must not.
                        self.progress.close()
                        exit_with(
                            {"status": "pending", "run_id": None, **self.context},
                            f"Another process took over this guarantee"
                            f"{self._item_label()}; re-run the same "
                            "command to join it.",
                            as_json=self.as_json,
                            code=EXIT_PENDING,
                        )
                    chunk = min(remaining, LEASE_RENEW_SECONDS)
                    try:
                        self.clock.sleep(chunk)
                    except KeyboardInterrupt:
                        # Known pre-submit: no POST in flight, so release the
                        # claim — the next run reclaims immediately instead
                        # of waiting out the lease grace.
                        self.store.cas(self.item_id, self.ticket_name, ticket, None)
                        raise
                    remaining -= chunk
                    if remaining <= 0:
                        break
                continue
            return submitted.json()["job_id"]

    def _fresh_ticket(self, *, claimed_at: float | None = None) -> dict:
        now = self.clock.now()
        return {
            "v": 1,  # ticket schema version, for future migration hooks
            # The verb, so read models can tell a pending parse item from a
            # pending extract item before any meta.json exists.
            "kind": self.kind,
            "job_id": None,
            "tier": self.tier,
            "params": self.params,
            "source": self.source,  # provenance for read models pre-completion
            "environment": self.environment,  # which API target this run addresses
            "submitted_at": now,
            # The claim generation: stable for the claim's whole life (unlike
            # submitted_at, which lease renewals rewrite). Idempotency keys
            # derive from it, so a resubmit that inherits a dead claim's
            # generation carries the same key.
            "claimed_at": now if claimed_at is None else claimed_at,
            "state": "pending",
        }

    def _update_ticket(self, expected: dict, new: dict) -> bool:
        """Compare-and-swap the claim ticket: publish ``new`` only while
        ``expected`` still owns the slot. False means the slot changed hands
        (a newer guarantee displaced us) and was left untouched."""
        return self.store.cas(self.item_id, self.ticket_name, expected, new)

    def _submit_owned(self, mine: dict) -> tuple[str, dict]:
        job_id = self._submit(mine)
        recorded = {**mine, "job_id": job_id}
        self._update_ticket(mine, recorded)
        # CAS failure ⇒ a different-params guarantee displaced us mid-submit;
        # our job still runs (and bills) — keep polling it, but its ticket now
        # belongs to the newer guarantee and later CAS writes will no-op.
        return job_id, recorded

    def _acquire_job(self) -> tuple[str, dict]:
        """Join or start the in-flight guarantee.

        The claim ticket is created atomically before submit: of any number
        of racing processes, exactly one submits; the rest join as pollers.
        A submitless ticket within its lease means an owner is inside submit
        (whatever its params) — waited on, never stolen; past the lease its
        owner has crashed and the slot is reclaimed. All ticket updates are
        compare-and-swap, so a displaced process can never clobber a newer
        guarantee's claim.
        """
        while True:
            ticket = self.store.read_json(self.item_id, self.ticket_name)
            if ticket is None:
                mine = self._fresh_ticket()
                if self.store.claim(self.item_id, self.ticket_name, mine):
                    return self._submit_owned(mine)
                continue  # lost the claim race: re-read and join
            if ticket.get("state") == "pending":
                if not ticket.get("job_id"):
                    age = self.clock.now() - (ticket.get("submitted_at") or 0)
                    if age < RECLAIM_GRACE_SECONDS:
                        # An owner (any params) is inside submit: wait for
                        # its job_id rather than racing it to the server.
                        self.clock.sleep(POLL_START_SECONDS)
                        continue
                    if ticket.get("params") == self.params:
                        # Its owner crashed inside submit — the server may
                        # have accepted the POST without us ever learning the
                        # job_id. Reclaim the slot atomically, inheriting the
                        # dead claim's generation: the resubmit then carries
                        # the same idempotency key and attaches to a job the
                        # server already accepted instead of duplicating it.
                        mine = self._fresh_ticket(claimed_at=ticket.get("claimed_at"))
                        if self._update_ticket(ticket, mine):
                            return self._submit_owned(mine)
                        continue
                elif ticket.get("params") == self.params:
                    return ticket["job_id"], ticket  # resume / join as poller
            elif (
                ticket.get("state") == "unreadable"
                and ticket.get("params") == self.params
                and not self.fresh
            ):
                # Completed but unreadable (a missing or unsupported
                # result): that job billed once, so join it and poll again —
                # a resubmit would bill a fresh job that reads the same way.
                return ticket["job_id"], ticket
            # Terminal, params-mismatch, lease-expired submitless, or
            # unreadable-under---force ticket: take the slot atomically, and
            # only if it is still the exact record we judged dead — a slot
            # that changed hands is left be.
            self.store.cas(self.item_id, self.ticket_name, ticket, None)
            continue
