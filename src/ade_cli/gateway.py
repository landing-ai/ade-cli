"""HTTP client for the ADE v2 job contracts.

Async job routes only (``POST /jobs`` + poll), never the sync routes.
Always sends ``service_tier`` (never the deprecated ``priority`` alias);
always ``Authorization: Bearer``, sourced per request from a ``BearerAuth``
so OAuth sessions can rotate the token mid-command (one refresh + one
retry after a 401; API keys never retry); always the ade-cli User-Agent
naming the invoking CLI command and the surface hosting it (built in
useragent.py, format in docs/user-agent.md) — ``command`` is a required
field, so an API-bound command cannot ship without its token.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx

from . import surface
from .useragent import user_agent


def _surface_pairs() -> tuple[tuple[str, str], ...]:
    """The ``host``/``term`` User-Agent tokens (#50): which surface hosts
    this invocation, from the same detection the usage ledger records.
    Detected per request — pure dict lookups, and cache-free so a faked
    test environment is honored — and never raising: identity must not
    be able to fail a request."""
    try:
        is_tty = sys.stdout.isatty()
    except Exception:
        is_tty = False
    detected = surface.detect(os.environ, stdout_is_tty=is_tty)
    pairs: list[tuple[str, str]] = []
    if detected.host is not None:
        pairs.append(("host", detected.host))
    pairs.append(("term", detected.term))
    return tuple(pairs)


class GatewayError(Exception):
    """A response outside the route's success contract."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        retry_after: float | None = None,
        code: str | None = None,
    ):
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after  # seconds, from a 429's Retry-After
        self.code = code  # the v2 error envelope's machine code, when sent


def _error_fields(response: httpx.Response) -> tuple[str, str | None]:
    """(detail, code) from an error body: the v2 envelope's ``{code,
    message}`` when present (a 422 names the rejected option key in
    ``message``), the transitional ``detail`` key otherwise, the auth
    layer's ``{error}`` envelope (its 401s) after that, raw text as the
    last resort."""
    try:
        body = response.json()
    except (json.JSONDecodeError, AttributeError):
        return response.text, None
    if isinstance(body, dict):
        if body.get("message"):
            code = body.get("code")
            return str(body["message"]), str(code) if code else None
        if body.get("detail"):
            return str(body["detail"]), None
        if body.get("error"):
            return str(body["error"]), None
    return response.text, None


def _check(response: httpx.Response, expected: tuple[int, ...]) -> httpx.Response:
    if response.status_code not in expected:
        detail, code = _error_fields(response)
        retry_after: float | None = None
        header = response.headers.get("Retry-After")
        if header is not None:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        raise GatewayError(response.status_code, detail, retry_after, code)
    return response


# Per-operation httpx timeout (connect / read / write / pool) — NOT a
# wall-clock bound on a whole request: a large upload making steady progress
# can legitimately run longer. Liveness of claim tickets is therefore NOT
# derived from this; the lease heartbeat in parse.py is what keeps a live
# owner's claim from being reclaimed.
REQUEST_TIMEOUT_SECONDS = 300.0

# The one authenticated route with no billing and no side effects for an
# empty batch — the usage-ledger upload (shipping.py POSTs backlogs here).
TELEMETRY_PATH = "/v2/telemetry"
# A login verification is interactive: a hung network should fail the
# probe in seconds, not sit on the job routes' 300s allowance.
VERIFY_TIMEOUT_SECONDS = 10.0


def verify_credential(
    *, endpoint: str, secret: str, transport: httpx.BaseTransport, command: str
) -> None:
    """Live-check a credential against the platform's auth gate: POST an
    empty batch to the telemetry route — authenticated like every API
    route, free, and a no-op body — and raise GatewayError on any
    non-200. A 401 is the platform's authoritative "invalid credential";
    anything else says nothing about the key.

    The request declares itself: a ``probe/auth`` User-Agent token (the
    grammar's documented extension seam — parsers ignore unknown tokens)
    marks it as a credential check, so the platform can tell these
    requests from real ledger uploads in its request logs."""
    with httpx.Client(
        base_url=endpoint,
        transport=transport,
        timeout=VERIFY_TIMEOUT_SECONDS,
        headers={
            "User-Agent": user_agent(
                ("command", command), ("probe", "auth"), *_surface_pairs()
            ),
            "X-Source": "cli",
            "Authorization": f"Bearer {secret}",
        },
    ) as client:
        _check(client.post(TELEMETRY_PATH, json=[]), (200,))


# The OAuth session's organization selection (ADR-0009). A user-scoped
# bearer token is organization-blind, so the platform's authz reads this
# header and verifies membership per request; without it, requests fall
# to the account's platform-side default organization. API keys are
# already organization-bound — callers pass no org and no header is sent.
ORG_ID_HEADER = "x-org-id"


class BearerAuth(Protocol):
    """Where the Authorization header comes from, request by request."""

    def token(self) -> str: ...

    def retry_after_401(self) -> str | None:
        """A fresh token to retry once with, or None when the credential
        cannot be refreshed (API keys)."""
        ...


@dataclass(frozen=True)
class StaticBearer:
    secret: str

    def token(self) -> str:
        return self.secret

    def retry_after_401(self) -> None:
        return None


@dataclass
class Gateway:
    endpoint: str
    auth: BearerAuth
    transport: httpx.BaseTransport
    # The CLI command this gateway serves, sent as the User-Agent's
    # ``command/<name>`` token — the invoking command, so a parse job run
    # inside `extract -d` still says command/extract.
    command: str
    # The selected organization id (OAuth sessions only; ADR-0009), sent
    # as ORG_ID_HEADER on every request.
    org_id: str | None = None

    def _send(
        self,
        request: Callable[[httpx.Client], httpx.Response],
        *,
        expected: tuple[int, ...],
    ) -> httpx.Response:
        # ``request`` builds the request from scratch each call, so the one
        # permitted 401 retry replays an identical body under the new token.
        with httpx.Client(
            base_url=self.endpoint,
            transport=self.transport,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "User-Agent": user_agent(
                    ("command", self.command), *_surface_pairs()
                ),
                # Declares the inference_history `source` (#49): the platform
                # relays X-Source verbatim into the recorded row's source
                # column, distinguishing CLI rows from raw-API ones.
                "X-Source": "cli",
                **({ORG_ID_HEADER: self.org_id} if self.org_id else {}),
            },
        ) as client:
            client.headers["Authorization"] = f"Bearer {self.auth.token()}"
            response = request(client)
            if response.status_code == 401:
                fresh = self.auth.retry_after_401()
                if fresh is not None:
                    client.headers["Authorization"] = f"Bearer {fresh}"
                    response = request(client)
            return _check(response, expected)

    def submit_parse(
        self,
        *,
        document: tuple[str, bytes] | None,
        document_url: str | None,
        model: str,
        service_tier: str,
        options: dict | None,
    ) -> httpx.Response:
        # Multipart per the contract; a URL-only submit still goes multipart
        # via a filename-less field. The caller passes the document bytes it
        # hashed, so identity and upload can never diverge.
        fields: dict[str, tuple[None, str]] = {}
        if document is not None:
            files: dict = {"document": document}
        else:
            assert document_url is not None
            files = {}
            fields["document_url"] = (None, document_url)
        fields["model"] = (None, model)
        fields["service_tier"] = (None, service_tier)
        if options:
            fields["options"] = (None, json.dumps(options))
        return self._send(
            lambda client: client.post("/v2/parse/jobs", files={**files, **fields}),
            expected=(202,),
        )

    def get_parse_job(self, job_id: str) -> httpx.Response:
        # 206 is a successful partial result, not an error.
        return self._send(
            lambda client: client.get(f"/v2/parse/jobs/{job_id}"),
            expected=(200, 206),
        )

    def submit_extract(
        self,
        *,
        schema: dict,
        markdown: str | None,
        markdown_upload: tuple[str, bytes] | None,
        markdown_url: str | None,
        model: str,
        service_tier: str,
        options: dict | None,
        idempotency_key: str,
    ) -> httpx.Response:
        # Exactly one markdown source, decided by the caller: inline JSON
        # below the staging threshold; above it, a multipart FILE part named
        # ``markdown`` — the contract's large-input path (the gateway stages
        # the upload internally; the public request takes no ``*_ref``
        # fields). URLs pass through. The idempotency key is derived from
        # the claim generation so a retried submit attaches to the job the
        # server already accepted (platform ask filed; harmless until
        # honored).
        headers = {"Idempotency-Key": idempotency_key}
        if markdown_upload is not None:
            # Multipart: non-file fields ride as JSON-serialized form
            # strings per the contract.
            fields: dict[str, tuple[None, str]] = {
                "schema": (None, json.dumps(schema)),
                "model": (None, model),
                "service_tier": (None, service_tier),
            }
            if options:
                fields["options"] = (None, json.dumps(options))
            return self._send(
                lambda client: client.post(
                    "/v2/extract/jobs",
                    files={"markdown": markdown_upload, **fields},
                    headers=headers,
                ),
                expected=(202,),
            )
        body: dict = {"schema": schema, "model": model, "service_tier": service_tier}
        if markdown is not None:
            body["markdown"] = markdown
        if markdown_url is not None:
            body["markdown_url"] = markdown_url
        if options:
            body["options"] = options
        return self._send(
            lambda client: client.post("/v2/extract/jobs", json=body, headers=headers),
            expected=(202,),
        )

    def get_extract_job(self, job_id: str) -> httpx.Response:
        return self._send(
            lambda client: client.get(f"/v2/extract/jobs/{job_id}"),
            expected=(200,),
        )
