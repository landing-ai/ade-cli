"""OAuth browser login: the full PKCE flow against the fake transport, with
a fake browser opener that "clicks allow" by hitting the real loopback
listener. Token-endpoint traffic rides the scripted FakeTransport; only the
loopback callback touches a (localhost-only) socket."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest

from parse_fixtures import completed_job

KEY = "sk-test-0123456789abcd"
CLIENT_ID = "cli-native-test"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def id_token(claims: dict) -> str:
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(claims).encode())
    return f"{header}.{body}.fake-signature"


def token_response(
    access: str = "at-1",
    refresh: str | None = "rt-1",
    email: str = "zhichao.lin@landing.ai",
    expires_in: int = 3600,
) -> dict:
    payload = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "id_token": id_token({"sub": "user-1", "email": email}),
    }
    if refresh is not None:
        payload["refresh_token"] = refresh
    return payload


def seed_oauth_config(cli, environment: str = "production", **overrides) -> None:
    """config.json carries the client id — the acceptance criterion is that
    real Logto config arrives as data, never code."""
    cli.home.mkdir(parents=True, exist_ok=True)
    config: dict = {"oauth": {environment: {"client_id": CLIENT_ID, **overrides}}}
    if environment != "production":
        config["environment"] = environment
    (cli.home / "config.json").write_text(json.dumps(config))


def seed_stored_oauth(
    cli,
    environment: str = "production",
    access: str = "at-old",
    refresh: str | None = "rt-old",
    expires_at: float = 1_750_000_000.0 + 3600,  # FakeClock start + 1h
) -> None:
    cli.home.mkdir(parents=True, exist_ok=True)
    entry = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "identity": {"sub": "user-1", "email": "zhichao.lin@landing.ai"},
    }
    (cli.home / "credentials.json").write_text(
        json.dumps({"environments": {environment: {"method": "oauth", "oauth": entry}}})
    )


class ClickAllow:
    """A fake browser: parse the authorize URL and complete the redirect the
    way a user clicking allow would. ``respond`` overrides the callback
    query (``{state}`` interpolates the real state, e.g. for a denial)."""

    def __init__(self, respond: str | None = None):
        self.url: str | None = None
        self.respond = respond

    def query(self) -> dict:
        assert self.url is not None
        return {k: v[0] for k, v in parse_qs(urlparse(self.url).query).items()}

    def __call__(self, url: str) -> bool:
        self.url = url
        q = self.query()
        template = self.respond or "code=code-1&state={state}"
        try:
            urlopen(f"{q['redirect_uri']}?{template.format(state=q['state'])}")
        except OSError:
            pass  # rejected callbacks (400) still count as delivered
        return True


def stored_environments(cli) -> dict:
    return json.loads((cli.home / "credentials.json").read_text())["environments"]


def test_login_pkce_flow_stores_tokens_0600(cli):
    seed_oauth_config(cli)
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", browser=browser)

    assert result.exit_code == 0, result.output
    # The authorize request carries the whole PKCE contract.
    q = browser.query()
    assert browser.url.startswith("https://login.landing.ai/oidc/auth?")
    assert q["client_id"] == CLIENT_ID
    assert q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256"
    assert q["resource"] == "https://api.ade.landing.ai"
    assert "offline_access" in q["scope"]
    assert q["prompt"] == "consent"
    assert q["redirect_uri"].startswith("http://127.0.0.1:")
    # The exchange went to the issuer's token endpoint with the matching
    # verifier: S256(code_verifier) must equal the challenge sent above.
    (exchange,) = cli.transport.requests
    assert str(exchange.url) == "https://login.landing.ai/oidc/token"
    form = {k: v[0] for k, v in parse_qs(exchange.content.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "code-1"
    challenge = _b64url(hashlib.sha256(form["code_verifier"].encode()).digest())
    assert challenge == q["code_challenge"]
    # Tokens stored per environment, file locked down.
    creds = cli.home / "credentials.json"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    entry = stored_environments(cli)["production"]
    assert entry["method"] == "oauth"
    assert entry["oauth"]["access_token"] == "at-1"
    assert entry["oauth"]["refresh_token"] == "rt-1"
    assert entry["oauth"]["identity"]["email"] == "zhichao.lin@landing.ai"


def test_login_json_reports_identity_without_leaking_tokens(cli):
    seed_oauth_config(cli)
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", "--json", browser=ClickAllow())

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["method"] == "oauth"
    assert payload["identity"]["email"] == "zhichao.lin@landing.ai"
    assert "at-1" not in result.stdout


def test_login_targets_the_env_flag(cli):
    seed_oauth_config(cli, environment="dev")
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", "--env", "dev", browser=browser)

    assert result.exit_code == 0
    assert browser.url.startswith("https://login.dev.landing.ai/oidc/auth?")
    assert browser.query()["resource"] == "https://api.ade.dev.landing.ai"
    assert stored_environments(cli)["dev"]["method"] == "oauth"


def test_login_env_with_a_stored_oauth_session_is_a_no_op(cli):
    # A stored session means `login --env X` has nothing to do: no browser,
    # no token exchange (ensure semantics).
    seed_stored_oauth(cli, environment="staging")
    browser = ClickAllow()

    result = cli.invoke("auth", "login", "--env", "staging", "--json", browser=browser)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["already_authenticated"] is True
    assert payload["method"] == "oauth"
    assert payload["environment"] == "staging"
    assert payload["identity"]["email"] == "zhichao.lin@landing.ai"
    assert browser.url is None  # browser never opened
    assert cli.transport.requests == []  # no token exchange


def test_flagless_browser_login_targets_production_despite_stale_config_keys(cli):
    # A pre-ADR-0003 config still naming an environment changes nothing:
    # resolution is per-invocation, so flagless means production.
    cli.home.mkdir(parents=True)
    (cli.home / "config.json").write_text(
        json.dumps(
            {"environment": "staging", "oauth": {"production": {"client_id": CLIENT_ID}}}
        )
    )
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", "--json", browser=browser)

    assert result.exit_code == 0
    assert browser.url.startswith("https://login.landing.ai/oidc/auth?")
    assert browser.query()["resource"] == "https://api.ade.landing.ai"
    assert json.loads(result.stdout)["environment"] == "production"
    assert stored_environments(cli)["production"]["method"] == "oauth"


def test_login_config_overrides_issuer_and_resource(cli):
    seed_oauth_config(
        cli,
        issuer="https://login.example.test/oidc",
        resource="https://api.example.test",
    )
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", browser=browser)

    assert result.exit_code == 0
    assert browser.url.startswith("https://login.example.test/oidc/auth?")
    assert browser.query()["resource"] == "https://api.example.test"
    (exchange,) = cli.transport.requests
    assert str(exchange.url) == "https://login.example.test/oidc/token"


def test_login_denied_errors_cleanly_without_tokens(cli):
    seed_oauth_config(cli)

    result = cli.invoke(
        "auth", "login", browser=ClickAllow(respond="error=access_denied&state={state}")
    )

    assert result.exit_code == 1
    assert "denied" in result.output.lower() or "access_denied" in result.output
    assert not (cli.home / "credentials.json").exists()
    assert cli.transport.requests == []  # no exchange was attempted


def test_forged_state_is_ignored_and_never_exchanged(cli):
    seed_oauth_config(cli)

    # The listener rejects the forged callback (400) and keeps waiting, so
    # the flow ends as a timeout — never a code exchange.
    result = cli.invoke(
        "auth", "login", browser=ClickAllow(respond="code=code-1&state=forged")
    )

    assert result.exit_code == 1
    assert "timed out" in result.output
    assert cli.transport.requests == []


def test_stray_probe_does_not_kill_the_login(cli):
    seed_oauth_config(cli)
    cli.transport.respond(200, token_response())

    class ProbeThenAllow(ClickAllow):
        def __call__(self, url: str) -> bool:
            self.url = url
            q = self.query()
            try:
                urlopen(q["redirect_uri"])  # a paramless localhost probe
            except OSError:
                pass
            return super().__call__(url)

    result = cli.invoke("auth", "login", browser=ProbeThenAllow())

    assert result.exit_code == 0, result.output
    assert stored_environments(cli)["production"]["method"] == "oauth"


class ClickAllowReadingPage(ClickAllow):
    """ClickAllow that also keeps the callback landing page's HTML."""

    body: str = ""

    def __call__(self, url: str) -> bool:
        self.url = url
        q = self.query()
        template = self.respond or "code=code-1&state={state}"
        with urlopen(f"{q['redirect_uri']}?{template.format(state=q['state'])}") as resp:
            self.body = resp.read().decode("utf-8")
        return True


def test_callback_page_is_the_branded_success_landing(cli):
    seed_oauth_config(cli)
    cli.transport.respond(200, token_response())
    browser = ClickAllowReadingPage()

    result = cli.invoke("auth", "login", browser=browser)

    assert result.exit_code == 0, result.output
    # The LandingAI-branded page, not the bare "Login complete" stub.
    assert 'class="success"' in browser.body
    assert "You&#x27;re signed in to" in browser.body
    assert "Agentic Document Extraction" in browser.body
    assert "return to the terminal" in browser.body
    assert "__HEADLINE__" not in browser.body  # placeholders all substituted


def test_callback_page_error_variant_escapes_idp_text(cli):
    seed_oauth_config(cli)
    browser = ClickAllowReadingPage(
        respond="error=access_denied&error_description=<b>denied</b>&state={state}"
    )

    result = cli.invoke("auth", "login", browser=browser)

    assert result.exit_code == 1
    assert 'class="error"' in browser.body
    assert "Sign-in didn&#x27;t complete" in browser.body
    # IdP-supplied description renders as text, never as markup.
    assert "&lt;b&gt;denied&lt;/b&gt;" in browser.body
    assert "<b>denied</b>" not in browser.body


def test_login_times_out_as_a_clean_failure(cli):
    seed_oauth_config(cli)

    result = cli.invoke("auth", "login", browser=lambda url: True)  # never clicks

    assert result.exit_code == 1
    assert "timed out" in result.output
    assert not (cli.home / "credentials.json").exists()


def test_login_headless_points_at_api_key(cli):
    seed_oauth_config(cli)

    result = cli.invoke("auth", "login", browser=lambda url: False)

    assert result.exit_code == 1
    assert "--api-key" in result.output


# --- Interactive method menu: choosing the browser, and when it's hidden ---


def test_arrow_menu_down_enter_picks_the_browser(cli):
    cli.stderr_tty = True
    cli.stdin_tty = True
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke(
        "auth", "login", "--json",
        keys=["\x1b[B", "\r"],
        env={"TERM": "xterm-256color"},
        browser=browser,
    )

    assert result.exit_code == 0, result.output
    assert "> 2) Sign in with your browser" in result.stderr
    assert browser.url.startswith("https://login.landing.ai/oidc/auth?")
    assert json.loads(result.stdout)["method"] == "oauth"


def test_menu_browser_choice_ignores_a_stale_stored_endpoint(cli):
    # A pre-ADR-0003 config's stored raw endpoint is dead config: the menu
    # still offers the browser and the login lands on production.
    cli.home.mkdir(parents=True, exist_ok=True)
    (cli.home / "config.json").write_text(
        json.dumps({"endpoint": "https://stored.example.com"})
    )
    cli.stderr_tty = True
    cli.stdin_tty = True
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke(
        "auth", "login", "--json",
        keys=["\x1b[B", "\r"],
        env={"TERM": "xterm-256color"},
        browser=browser,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["method"] == "oauth"
    assert payload["environment"] == "production"
    assert browser.query()["resource"] == "https://api.ade.landing.ai"


def test_arrow_menu_escape_aborts_before_any_flow(cli):
    cli.stderr_tty = True
    cli.stdin_tty = True

    result = cli.invoke(
        "auth", "login",
        keys=["\x1b"],
        env={"TERM": "xterm-256color"},
    )

    assert result.exit_code != 0
    assert not (cli.home / "credentials.json").exists()
    assert not (cli.home / "config.json").exists()  # target never persisted


def test_tty_menu_choice_2_runs_the_browser_flow(cli):
    cli.stderr_tty = True
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", "--json", input="2\n", browser=browser)

    assert result.exit_code == 0, result.output
    assert "How would you like to log in?" in result.stderr
    assert browser.url.startswith("https://login.landing.ai/oidc/auth?")
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["method"] == "oauth"
    assert payload["environment"] == "production"


def test_non_tty_login_skips_the_menu_and_goes_to_browser(cli):
    # The scripted/CI shape: no terminal, no prompts — straight to the
    # browser flow, exactly the pre-menu behavior.
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", "--json", browser=browser)

    assert result.exit_code == 0, result.output
    assert "How would you like to log in?" not in result.stderr
    assert json.loads(result.stdout)["method"] == "oauth"


def test_tty_menu_collapses_to_api_key_when_oauth_is_unconfigured(cli, monkeypatch):
    # The API-key-only launch shape: an environment with no client_id
    # (simulated by stripping the baked default) offers no dead browser
    # option — the menu collapses straight into the key prompt.
    from ade_cli import oauth

    monkeypatch.delitem(oauth._CLIENT_IDS, "production")
    cli.stderr_tty = True

    cli.transport.respond(200, {"accepted": 0})  # the verification probe (#117)
    result = cli.invoke("auth", "login", "--json", input=KEY + "\n")

    assert result.exit_code == 0, result.output
    assert "How would you like to log in?" not in result.stderr
    assert "Browser sign-in isn't available" in result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["method"] == "api_key"
    assert stored_environments(cli)["production"]["api_key"] == KEY


def test_tty_menu_hides_browser_under_ade_endpoint_without_a_resource(cli):
    # A raw ADE_ENDPOINT has no environment to infer the token audience
    # from, so without a resource override the browser option cannot work
    # and only the key prompt appears.
    cli.stderr_tty = True

    cli.transport.respond(200, {"accepted": 0})  # the verification probe (#117)
    result = cli.invoke(
        "auth", "login", "--json",
        input=KEY + "\n",
        env={"ADE_ENDPOINT": "https://custom.example.com"},
    )

    assert result.exit_code == 0, result.output
    assert "How would you like to log in?" not in result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["method"] == "api_key"
    assert payload["endpoint"] == "https://custom.example.com"


def test_tty_menu_offers_browser_under_ade_endpoint_with_a_resource(cli):
    cli.home.mkdir(parents=True, exist_ok=True)
    (cli.home / "config.json").write_text(
        json.dumps(
            {
                "oauth": {
                    "production": {
                        "client_id": CLIENT_ID,
                        "resource": "https://custom.example.com",
                    }
                }
            }
        )
    )
    cli.stderr_tty = True
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke(
        "auth", "login", "--json",
        input="2\n", browser=browser,
        env={"ADE_ENDPOINT": "https://custom.example.com"},
    )

    assert result.exit_code == 0, result.output
    assert "How would you like to log in?" in result.stderr
    assert browser.query()["resource"] == "https://custom.example.com"
    assert json.loads(result.stdout[result.stdout.index("{"):])["method"] == "oauth"


# Every environment's registered client id is a baked default: browser
# login must survive a wiped ~/.ade (config.json holds an override, not a
# prerequisite).
BAKED_CLIENT_IDS = {
    "dev": "7zs0x5fjag7mhm6z4jbjh",
    "staging": "ajuo8ch2yle7xu8fvsz3c",
    "production": "a7k31qip5bylclf3kfgdg",
    "eu": "3i9hgicjpdh0ibsiq3ri4",
}


@pytest.mark.parametrize("environment", sorted(BAKED_CLIENT_IDS))
def test_every_environment_logs_in_with_no_config_at_all(cli, environment):
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", "--env", environment, browser=browser)

    assert result.exit_code == 0, result.output
    assert browser.query()["client_id"] == BAKED_CLIENT_IDS[environment]
    assert stored_environments(cli)[environment]["method"] == "oauth"


def test_unregistered_environment_says_how_to_configure(cli, monkeypatch):
    # The oauth_not_configured remediation guards environments added to
    # ENVIRONMENTS before their Logto registration lands. No shipped
    # environment is in that state anymore, so simulate one: strip the
    # baked default (the one non-CLI seam in this file, per review).
    from ade_cli import oauth

    monkeypatch.delitem(oauth._CLIENT_IDS, "production")

    result = cli.invoke("auth", "login", browser=lambda url: True)

    assert result.exit_code == 1
    assert "client_id" in result.output
    assert "--api-key" in result.output
    assert not (cli.home / "credentials.json").exists()


def test_config_client_id_overrides_the_baked_default(cli):
    seed_oauth_config(cli, environment="dev")  # seeds CLIENT_ID, not the baked id
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke("auth", "login", "--env", "dev", browser=browser)

    assert result.exit_code == 0
    assert browser.query()["client_id"] == CLIENT_ID


def test_failed_login_does_not_switch_the_environment(cli):
    cli.home.mkdir(parents=True, exist_ok=True)
    config_body = json.dumps({"oauth": {"staging": {"client_id": CLIENT_ID}}})
    (cli.home / "config.json").write_text(config_body)

    result = cli.invoke(
        "auth", "login", "--env", "staging",
        browser=ClickAllow(respond="error=access_denied&state={state}"),
    )

    assert result.exit_code == 1
    # The denial left the configured target exactly as it was.
    assert json.loads((cli.home / "config.json").read_text()) == json.loads(config_body)


def test_browser_login_under_ade_endpoint_requires_an_explicit_resource(cli):
    cli.home.mkdir(parents=True, exist_ok=True)
    (cli.home / "config.json").write_text(
        json.dumps({"oauth": {"production": {"client_id": CLIENT_ID}}})
    )

    result = cli.invoke(
        "auth", "login",
        browser=lambda url: True,
        env={"ADE_ENDPOINT": "https://custom.example.com"},
    )

    assert result.exit_code == 1
    assert "resource" in result.output
    assert "--api-key" in result.output


def test_browser_login_under_ade_endpoint_with_resource_override_proceeds(cli):
    cli.home.mkdir(parents=True, exist_ok=True)
    (cli.home / "config.json").write_text(
        json.dumps(
            {
                "oauth": {
                    "production": {
                        "client_id": CLIENT_ID,
                        "resource": "https://custom.example.com",
                    }
                }
            }
        )
    )
    browser = ClickAllow()
    cli.transport.respond(200, token_response())

    result = cli.invoke(
        "auth", "login",
        browser=browser,
        env={"ADE_ENDPOINT": "https://custom.example.com"},
    )

    assert result.exit_code == 0, result.output
    assert browser.query()["resource"] == "https://custom.example.com"


def test_status_shows_oauth_identity_and_expiry(cli):
    seed_oauth_config(cli)
    cli.transport.respond(200, token_response())
    cli.invoke("auth", "login", browser=ClickAllow())

    result = cli.invoke("auth", "status", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is True
    assert payload["method"] == "oauth"
    assert payload["identity"]["email"] == "zhichao.lin@landing.ai"
    assert payload["expires_at"] == 1_750_000_000.0 + 3600
    assert "at-1" not in result.stdout


def test_status_human_output_names_the_identity(cli):
    seed_oauth_config(cli)
    cli.transport.respond(200, token_response())
    cli.invoke("auth", "login", browser=ClickAllow())

    result = cli.invoke("auth", "status")

    assert result.exit_code == 0
    assert "zhichao.lin@landing.ai" in result.stdout
    assert "expires" in result.stdout


def test_env_api_key_still_wins_over_stored_oauth(cli):
    seed_stored_oauth(cli)

    result = cli.invoke("auth", "status", "--json", env={"ADE_API_KEY": KEY})

    payload = json.loads(result.stdout)
    assert payload["method"] == "api_key"
    assert payload["source"] == "env"


def test_most_recent_login_wins_api_key_after_oauth(cli):
    seed_oauth_config(cli)
    cli.transport.respond(200, token_response())
    cli.invoke("auth", "login", browser=ClickAllow())

    cli.transport.respond(200, {"accepted": 0})  # the verification probe (#117)
    cli.invoke("auth", "login", "--api-key", KEY)
    result = cli.invoke("auth", "status", "--json")

    assert json.loads(result.stdout)["method"] == "api_key"


def test_logout_revokes_refresh_token_then_clears(cli):
    seed_oauth_config(cli)
    seed_stored_oauth(cli)
    cli.transport.respond(200, {})

    result = cli.invoke("auth", "logout", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "logged_out": True,
        "cleared": True,
        "revoked": 1,
        "scope": "environment",
        "environment": "production",
    }
    (revocation,) = cli.transport.requests
    assert str(revocation.url) == "https://login.landing.ai/oidc/token/revocation"
    form = {k: v[0] for k, v in parse_qs(revocation.content.decode()).items()}
    assert form["token"] == "rt-old"
    assert not (cli.home / "credentials.json").exists()


def test_logout_still_clears_when_revocation_fails(cli):
    seed_oauth_config(cli)
    seed_stored_oauth(cli)
    cli.transport.respond(500, {"error": "server_error"})

    result = cli.invoke("auth", "logout", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["cleared"] is True
    assert payload["revoked"] == 0
    assert not (cli.home / "credentials.json").exists()


# --- silent refresh on the network verbs ---


def _parse_args(tmp_path) -> tuple[str, ...]:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    return ("parse", "-d", str(doc), "--json")


def _form(request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_expired_access_token_refreshes_before_the_request(cli, tmp_path):
    seed_oauth_config(cli)
    seed_stored_oauth(cli, expires_at=1_750_000_000.0 - 10)  # already expired
    cli.transport.respond(200, token_response(access="at-2", refresh="rt-2"))
    cli.transport.respond(202, {"job_id": "job-0001", "status": "pending"})
    cli.transport.respond(200, completed_job())

    result = cli.invoke(*_parse_args(tmp_path))

    assert result.exit_code == 0, result.output
    refresh, submit, poll = cli.transport.requests
    assert str(refresh.url) == "https://login.landing.ai/oidc/token"
    assert _form(refresh)["grant_type"] == "refresh_token"
    assert _form(refresh)["refresh_token"] == "rt-old"
    assert submit.headers["Authorization"] == "Bearer at-2"
    assert poll.headers["Authorization"] == "Bearer at-2"
    # Rotation: the new refresh token replaced the spent one on disk.
    entry = stored_environments(cli)["production"]
    assert entry["oauth"]["refresh_token"] == "rt-2"


def test_401_triggers_one_refresh_and_retry(cli, tmp_path):
    seed_oauth_config(cli)
    seed_stored_oauth(cli)  # not expired — the server invalidated it anyway
    cli.transport.respond(401, {"detail": "token revoked"})
    cli.transport.respond(200, token_response(access="at-2", refresh="rt-2"))
    cli.transport.respond(202, {"job_id": "job-0001", "status": "pending"})
    cli.transport.respond(200, completed_job())

    result = cli.invoke(*_parse_args(tmp_path))

    assert result.exit_code == 0, result.output
    first_submit, refresh, retried_submit, poll = cli.transport.requests
    assert first_submit.headers["Authorization"] == "Bearer at-old"
    assert _form(refresh)["grant_type"] == "refresh_token"
    assert retried_submit.headers["Authorization"] == "Bearer at-2"
    assert retried_submit.url == first_submit.url


def test_failed_refresh_says_relogin(cli, tmp_path):
    seed_oauth_config(cli)
    seed_stored_oauth(cli, expires_at=1_750_000_000.0 - 10)
    cli.transport.respond(400, {"error": "invalid_grant"})

    result = cli.invoke(*_parse_args(tmp_path))

    assert result.exit_code == 1
    assert "auth login" in result.output


def test_refresh_without_refresh_token_says_relogin(cli, tmp_path):
    seed_oauth_config(cli)
    seed_stored_oauth(cli, refresh=None, expires_at=1_750_000_000.0 - 10)

    result = cli.invoke(*_parse_args(tmp_path))

    assert result.exit_code == 1
    assert "auth login" in result.output


def test_api_key_requests_never_touch_the_token_endpoint(cli, tmp_path):
    cli.transport.respond(200, {"accepted": 0})  # the verification probe (#117)
    cli.invoke("auth", "login", "--api-key", KEY)
    cli.transport.respond(202, {"job_id": "job-0001", "status": "pending"})
    cli.transport.respond(200, completed_job())

    result = cli.invoke(*_parse_args(tmp_path))

    assert result.exit_code == 0, result.output
    _probe, submit, poll = cli.transport.requests
    assert submit.headers["Authorization"] == f"Bearer {KEY}"
