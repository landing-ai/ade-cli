import json
import stat

KEY = "sk-test-0123456789abcd"


def credentials_file(cli):
    return cli.home / "credentials.json"


def test_login_with_api_key_flag_stores_credential_in_0600_file(cli):
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", KEY)

    assert result.exit_code == 0
    creds = credentials_file(cli)
    assert creds.exists()
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert KEY in creds.read_text()


def test_login_keeps_credentials_separate_from_endpoint_config(cli):
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", KEY)

    assert result.exit_code == 0
    # The key lands in credentials.json, never config.json.
    assert KEY in credentials_file(cli).read_text()
    config = config_file(cli)
    assert not config.exists() or KEY not in config.read_text()


def test_login_api_key_dash_prompts_hidden(cli):
    # `--api-key -` is the direct interactive spelling (a flagless terminal
    # login reaches the same key prompt via the method menu).
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", "-", input=KEY + "\n")

    assert result.exit_code == 0
    assert KEY in credentials_file(cli).read_text()
    assert KEY not in result.stdout  # hidden input never echoes


def test_login_prompt_with_json_keeps_stdout_one_stable_object(cli):
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", "-", "--json", input=KEY + "\n")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)  # prompt text must not pollute stdout
    assert payload["stored"] is True


def test_login_json_confirms_without_leaking_the_full_key(cli):
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", KEY, "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["method"] == "api_key"
    assert KEY not in result.stdout
    assert payload["credential"].endswith(KEY[-4:])


def test_status_reports_stored_key_masked_with_source(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)

    result = cli.invoke("auth", "status", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is True
    assert payload["method"] == "api_key"
    assert payload["source"] == "stored"
    assert payload["credential"].endswith(KEY[-4:])
    assert KEY not in result.stdout


def test_env_api_key_wins_over_stored_and_status_says_so(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)
    env_key = "sk-env-9999wxyz"

    result = cli.invoke("auth", "status", "--json", env={"ADE_API_KEY": env_key})

    payload = json.loads(result.stdout)
    assert payload["source"] == "env"
    assert payload["credential"].endswith(env_key[-4:])


def test_status_without_credentials_is_a_distinct_exit_state(cli):
    result = cli.invoke("auth", "status", "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["authenticated"] is False


def test_logout_clears_stored_credentials(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)

    result = cli.invoke("auth", "logout", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "logged_out": True,
        "cleared": True,
        "revoked": 0,
        "scope": "environment",
        "environment": "production",
    }
    assert not credentials_file(cli).exists()
    assert cli.invoke("auth", "status").exit_code == 1


def test_top_level_login_and_logout_alias_the_auth_commands(cli):
    # `ade login` / `ade logout` are the same callbacks registered at the
    # root — flags and payloads must match the `auth` spellings exactly.
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("login", "--api-key", KEY, "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["stored"] is True
    assert KEY in credentials_file(cli).read_text()

    result = cli.invoke("logout", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["cleared"] is True
    assert not credentials_file(cli).exists()


def test_logout_is_idempotent(cli):
    result = cli.invoke("auth", "logout", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "logged_out": True,
        "cleared": False,
        "revoked": 0,
        "scope": "environment",
        "environment": "production",
    }


def test_ade_endpoint_overrides_the_environment_endpoint(cli):
    plain = cli.invoke("auth", "status", "--json", env={"ADE_API_KEY": KEY})
    overridden = cli.invoke(
        "auth",
        "status",
        "--json",
        env={"ADE_API_KEY": KEY, "ADE_ENDPOINT": "https://env.example.com"},
    )

    assert json.loads(plain.stdout)["endpoint"] == "https://api.ade.landing.ai"
    assert json.loads(plain.stdout)["endpoint_source"] == "default"
    assert json.loads(overridden.stdout)["endpoint"] == "https://env.example.com"
    assert json.loads(overridden.stdout)["endpoint_source"] == "env"


def test_ade_endpoint_trailing_slash_is_normalized(cli):
    payload = json.loads(
        cli.invoke(
            "auth", "status", "--json",
            env={"ADE_API_KEY": KEY, "ADE_ENDPOINT": "https://staging.example.com/"},
        ).stdout
    )

    assert payload["endpoint"] == "https://staging.example.com"


def test_flagless_interactive_login_targets_the_default_without_pinning(cli):
    # The key prompt is the only prompt: the target is never asked for —
    # flagless means production everywhere, and --env (or ADE_ENV) names
    # anything else (one rule, same as the browser path).
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", "-", "--json", input=KEY + "\n")

    assert result.exit_code == 0
    assert "Environment" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["endpoint"] == "https://api.ade.landing.ai"
    assert payload["endpoint_source"] == "default"
    assert not (cli.home / "config.json").exists()


def test_endpoint_defaults_to_the_canonical_host(cli):
    result = cli.invoke("auth", "status", "--json", env={"ADE_API_KEY": KEY})

    payload = json.loads(result.stdout)
    assert payload["endpoint"] == "https://api.ade.landing.ai"
    assert payload["endpoint_source"] == "default"


def test_status_human_output_names_method_source_and_masked_credential(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)

    result = cli.invoke("auth", "status")

    assert result.exit_code == 0
    assert "api_key" in result.stdout
    assert KEY[-4:] in result.stdout
    assert "stored" in result.stdout
    assert KEY not in result.stdout


def test_most_recent_login_wins(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)
    newer = "sk-newer-5678efgh"

    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", newer)

    result = cli.invoke("auth", "status", "--json")
    assert json.loads(result.stdout)["credential"].endswith(newer[-4:])
    assert stat.S_IMODE(credentials_file(cli).stat().st_mode) == 0o600


# --- Login verifies the key before storing it (#117) ----------------------


def test_login_verifies_the_key_with_an_empty_telemetry_batch(cli):
    cli.transport.respond(200, {"accepted": 0})

    result = cli.invoke("auth", "login", "--api-key", KEY, "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["verified"] is True
    (probe,) = cli.transport.requests
    assert probe.method == "POST"
    assert probe.url == "https://api.ade.landing.ai/v2/telemetry"
    assert json.loads(probe.content) == []  # a no-op body: nothing recorded
    assert probe.headers["Authorization"] == f"Bearer {KEY}"
    assert probe.headers["X-Source"] == "cli"
    assert "command/auth" in probe.headers["User-Agent"]
    # The probe declares itself, so the platform can tell a credential
    # check from a real ledger upload in its request logs.
    assert "probe/auth" in probe.headers["User-Agent"]


def test_login_verifies_against_the_resolved_environment(cli):
    cli.transport.respond(200, {"accepted": 0})

    result = cli.invoke("auth", "login", "--api-key", KEY, "--env", "staging")

    assert result.exit_code == 0
    (probe,) = cli.transport.requests
    assert str(probe.url).startswith("https://api.ade.staging.landing.ai")


def test_login_with_a_rejected_key_stores_nothing(cli):
    cli.transport.respond(401, {"error": "Invalid API Key Format"})

    result = cli.invoke("auth", "login", "--api-key", "not-a-real-key", "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "invalid_api_key"
    assert payload["status_code"] == 401
    assert payload["environment"] == "production"
    assert not credentials_file(cli).exists()


def test_rejected_key_message_is_canonical_whatever_the_server_said(cli):
    # The platform's 401 bodies vary by which check rejected the key; the
    # login line must not parrot them (#117's inconsistency report).
    bodies = [
        {"error": "Invalid API Key Format"},
        {"error": "Invalid API Key, please check that your API key is "
         "complete and entered correctly."},
    ]
    lines = []
    for body in bodies:
        cli.transport.respond(401, body)
        result = cli.invoke("auth", "login", "--api-key", "bad-key")
        assert result.exit_code == 1
        lines.append(result.stdout)

    assert lines[0] == lines[1]
    assert "This API key was rejected" in lines[0]
    assert "Nothing was stored" in lines[0]
    assert "Invalid API Key Format" not in lines[0]


def test_login_platform_error_is_not_blamed_on_the_key(cli):
    cli.transport.respond(503, {"message": "upstream unavailable"})

    result = cli.invoke("auth", "login", "--api-key", KEY, "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "verification_failed"
    assert payload["status_code"] == 503
    assert payload["message"] == "upstream unavailable"
    assert not credentials_file(cli).exists()


def test_login_platform_error_human_text_says_try_again_later(cli):
    cli.transport.respond(500, {"message": "boom"})

    result = cli.invoke("auth", "login", "--api-key", KEY)

    assert result.exit_code == 1
    assert "HTTP 500: boom" in result.stdout
    assert "try again later" in result.stdout
    assert "Nothing was stored" in result.stdout


def test_login_with_an_unreachable_endpoint_stores_nothing(cli):
    import httpx

    def offline(request):
        raise httpx.ConnectError("no network")

    cli.transport.respond_with(offline)

    result = cli.invoke("auth", "login", "--api-key", KEY, "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "verification_unreachable"
    assert not credentials_file(cli).exists()
    assert "no network" in payload["message"]


def test_login_verification_runs_even_with_telemetry_opted_out(cli):
    # ADE_TELEMETRY=0 / DO_NOT_TRACK govern the usage ledger and its
    # upload, not the auth check: the probe ships no telemetry data ([]
    # records nothing), so opting out of tracking must not silently
    # bring back the store-an-unchecked-key behavior (#117).
    cli.transport.respond(401, {"error": "Invalid API Key Format"})

    result = cli.invoke(
        "auth", "login", "--api-key", "bad-key", "--json",
        env={"ADE_TELEMETRY": "0", "DO_NOT_TRACK": "1"},
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "invalid_api_key"
    assert not credentials_file(cli).exists()


def test_opted_out_login_still_stores_a_key_the_platform_accepted(cli):
    cli.transport.respond(200, {"accepted": 0})

    result = cli.invoke(
        "auth", "login", "--api-key", KEY, "--json",
        env={"ADE_TELEMETRY": "0", "DO_NOT_TRACK": "1"},
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["verified"] is True
    assert KEY in credentials_file(cli).read_text()
    # The probe was the only request — no ledger event existed to ship.
    (probe,) = cli.transport.requests
    assert json.loads(probe.content) == []


def test_a_piped_key_is_verified_too(cli):
    cli.transport.respond(401, {"error": "Invalid API Key Format"})

    result = cli.invoke("auth", "login", "--json", input="bad-key\n")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "invalid_api_key"
    assert not credentials_file(cli).exists()


def test_login_rejects_an_empty_key(cli):
    result = cli.invoke("auth", "login", "--api-key", "")

    assert result.exit_code == 2
    assert not credentials_file(cli).exists()


def test_aborted_prompt_stores_nothing(cli):
    cli.stderr_tty = True

    # Menu default (API key) ⇒ hidden key prompt ⇒ EOF aborts the login.
    result = cli.invoke("auth", "login", input="\n")

    assert result.exit_code != 0
    assert not credentials_file(cli).exists()
    assert not config_file(cli).exists()  # the target was never persisted either


# --- Named environments (issue #32) ---------------------------------------

ENVIRONMENT_URLS = {
    "dev": "https://api.ade.dev.landing.ai",
    "staging": "https://api.ade.staging.landing.ai",
    "production": "https://api.ade.landing.ai",
    "eu": "https://api.ade.eu-west-1.landing.ai",
}


def config_file(cli):
    return cli.home / "config.json"


def test_every_named_environment_maps_to_its_endpoint(cli):
    for name, url in ENVIRONMENT_URLS.items():
        cli.transport.respond(200, {"accepted": 0})
        result = cli.invoke(
            "auth", "login", "--api-key", KEY, "--env", name, "--json"
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["environment"] == name
        assert payload["endpoint"] == url


def test_login_env_stores_only_the_credential_never_config(cli):
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--api-key", KEY, "--env", "staging", "--json"
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["endpoint_source"] == "environment"
    assert not config_file(cli).exists()  # nothing about the target persists
    creds = json.loads(credentials_file(cli).read_text())
    assert creds["environments"]["staging"]["api_key"] == KEY


def test_keys_are_stored_per_environment_and_do_not_cross(cli):
    staging_key = "sk-staging-1111aaaa"
    dev_key = "sk-dev-2222bbbb"
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", staging_key, "--env", "staging")
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", dev_key, "--env", "dev")

    on_dev = json.loads(cli.invoke("auth", "status", "--env", "dev", "--json").stdout)
    on_staging = json.loads(
        cli.invoke("auth", "status", "--env", "staging", "--json").stdout
    )

    assert on_dev["environment"] == "dev"
    assert on_dev["credential"].endswith(dev_key[-4:])
    assert on_staging["environment"] == "staging"
    assert on_staging["credential"].endswith(staging_key[-4:])


def test_an_environment_without_its_own_key_is_unauthenticated(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY, "--env", "staging")

    result = cli.invoke("auth", "status", "--json", env={"ADE_ENV": "eu"})

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is False
    assert payload["environment"] == "eu"
    assert payload["endpoint_source"] == "environment"
    human = cli.invoke("auth", "status", env={"ADE_ENV": "eu"}).stdout
    assert "the eu environment" in human  # names the environment missing a key
    # The remediation names --env so it re-auths this exact target.
    assert "ade auth login --env eu" in human


def test_unauthenticated_status_names_a_raw_url_target_not_an_environment(cli):
    result = cli.invoke(
        "auth", "status", env={"ADE_ENDPOINT": "https://custom.example.com"}
    )

    assert result.exit_code == 1
    assert "https://custom.example.com" in result.stdout
    # The credential namespace is production, but the user is pointing at a
    # raw URL — naming an environment here would mislead.
    assert "production environment" not in result.stdout


def test_legacy_single_key_credentials_still_authenticate(cli):
    cli.home.mkdir(parents=True)
    credentials_file(cli).write_text(
        json.dumps({"method": "api_key", "api_key": KEY})
    )

    result = cli.invoke("auth", "status", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["environment"] == "production"
    assert payload["credential"].endswith(KEY[-4:])


def test_legacy_key_still_serves_a_raw_endpoint_override(cli):
    cli.home.mkdir(parents=True)
    credentials_file(cli).write_text(
        json.dumps({"method": "api_key", "api_key": KEY})
    )

    payload = json.loads(
        cli.invoke(
            "auth", "status", "--json",
            env={"ADE_ENDPOINT": "https://stored.example.com"},
        ).stdout
    )

    assert payload["authenticated"] is True
    assert payload["endpoint"] == "https://stored.example.com"
    assert payload["endpoint_source"] == "env"


def test_env_login_migrates_a_legacy_key_under_production(cli):
    cli.home.mkdir(parents=True)
    credentials_file(cli).write_text(
        json.dumps({"method": "api_key", "api_key": KEY})
    )
    staging_key = "sk-staging-1111aaaa"

    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--api-key", staging_key, "--env", "staging"
    )

    assert result.exit_code == 0
    creds = json.loads(credentials_file(cli).read_text())
    assert creds["environments"]["production"]["api_key"] == KEY
    assert creds["environments"]["staging"]["api_key"] == staging_key
    assert "api_key" not in creds  # legacy top-level field fully migrated


def test_stale_selection_keys_in_config_are_ignored(cli):
    # Pre-ADR-0003 configs stored a selection; resolution is per-invocation
    # now, so leftover keys change nothing (config.json is oauth-only).
    cli.home.mkdir(parents=True)
    config_file(cli).write_text(
        json.dumps(
            {"environment": "staging", "endpoint": "https://stored.example.com"}
        )
    )

    payload = json.loads(
        cli.invoke("auth", "status", "--json", env={"ADE_API_KEY": KEY}).stdout
    )

    assert payload["environment"] == "production"
    assert payload["endpoint"] == "https://api.ade.landing.ai"


def test_unknown_ade_env_is_a_loud_error_naming_the_source(cli):
    result = cli.invoke(
        "auth", "status", "--json", env={"ADE_ENV": "qa", "ADE_API_KEY": KEY}
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "unknown_environment"
    assert payload["source"] == "ADE_ENV"


def test_login_rejects_an_unknown_environment_and_lists_the_choices(cli):
    result = cli.invoke("auth", "login", "--api-key", KEY, "--env", "qa")

    assert result.exit_code == 2
    assert "staging" in result.stdout  # the valid set, right in the error
    assert not credentials_file(cli).exists()


def test_ade_endpoint_env_var_wins_over_a_named_environment(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY, "--env", "staging")

    payload = json.loads(
        cli.invoke(
            "auth", "status", "--env", "staging", "--json",
            env={"ADE_ENDPOINT": "https://env.example.com"},
        ).stdout
    )

    assert payload["endpoint"] == "https://env.example.com"
    assert payload["endpoint_source"] == "env"
    # Credentials still file under the configured environment.
    assert payload["environment"] == "staging"
    assert payload["credential"].endswith(KEY[-4:])


def test_ade_env_targets_the_login_and_the_flag_beats_it(cli):
    # One resolution rule on every command: --env → ADE_ENV → production.
    cli.transport.respond(200, {"accepted": 0})
    ambient = cli.invoke(
        "auth", "login", "--api-key", KEY, "--json", env={"ADE_ENV": "staging"}
    )
    cli.transport.respond(200, {"accepted": 0})
    flagged = cli.invoke(
        "auth", "login", "--api-key", KEY, "--env", "dev", "--json",
        env={"ADE_ENV": "staging"},
    )

    assert json.loads(ambient.stdout)["environment"] == "staging"
    assert json.loads(flagged.stdout)["environment"] == "dev"
    creds = json.loads(credentials_file(cli).read_text())["environments"]
    assert set(creds) == {"staging", "dev"}


def test_flagless_logout_honors_ade_env(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY, "--env", "staging")
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)  # production too

    result = cli.invoke("auth", "logout", "--json", env={"ADE_ENV": "staging"})

    payload = json.loads(result.stdout)
    assert payload["environment"] == "staging"
    creds = json.loads(credentials_file(cli).read_text())["environments"]
    assert "staging" not in creds and "production" in creds


def test_interactive_login_targets_an_environment_via_the_flag(cli):
    # The target is flags-only: `--env` names it and the key prompt is the
    # only question asked.
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--api-key", "-", "--env", "staging", "--json",
        input=KEY + "\n",
    )

    assert result.exit_code == 0
    assert "Environment" not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["environment"] == "staging"
    assert payload["endpoint"] == "https://api.ade.staging.landing.ai"


def test_interactive_login_defaults_to_production(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY, "--env", "eu")

    # Flagless targets production (the stable default), not the sticky
    # current environment (issue #72) — with no prompt asking about it.
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", "-", "--json", input=KEY + "\n")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["environment"] == "production"


# --- Interactive method menu: a terminal login picks api key vs browser ----


def test_arrow_menu_enter_alone_continues_with_api_key(cli):
    # A real terminal (stdin included) gets the arrow selector: bare Enter
    # confirms the default, so nobody has to type a number.
    cli.stderr_tty = True
    cli.stdin_tty = True

    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--json",
        input=KEY + "\n",
        keys=["\r"],
        env={"TERM": "xterm-256color"},
    )

    assert result.exit_code == 0, result.output
    assert "↑/↓" in result.stderr
    assert "> 1) Paste an API key" in result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["method"] == "api_key"
    assert payload["environment"] == "production"


def test_arrow_menu_falls_back_to_typed_when_raw_mode_fails(cli):
    # stdin claims to be a tty but raw reads fail (odd wrappers): the
    # numbered prompt takes over in the same run and reads regular stdin.
    cli.stderr_tty = True
    cli.stdin_tty = True

    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--json",
        input="1\n" + KEY + "\n",
        keys=[OSError("stdin has no raw mode")],
        env={"TERM": "xterm-256color"},
    )

    assert result.exit_code == 0, result.output
    assert "Method" in result.stderr  # the typed prompt took over
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["method"] == "api_key"


def test_dumb_terminal_skips_the_arrow_selector(cli):
    cli.stderr_tty = True
    cli.stdin_tty = True

    # No keys scripted: engaging the selector would be a harness failure,
    # so success proves TERM=dumb routed straight to the typed prompt.
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--json",
        input="\n" + KEY + "\n",
        env={"TERM": "dumb"},
    )

    assert result.exit_code == 0, result.output
    assert "↑/↓" not in result.stderr
    assert json.loads(result.stdout[result.stdout.index("{"):])["method"] == "api_key"


def test_tty_login_menus_and_defaults_to_api_key(cli):
    cli.stderr_tty = True

    # Accept the menu default (1 = API key), then give the key — nothing
    # else is asked.
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--json", input="\n" + KEY + "\n"
    )

    assert result.exit_code == 0, result.output
    assert "How would you like to log in?" in result.stderr
    assert "1) Paste an API key" in result.stderr
    assert "2) Sign in with your browser" in result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["method"] == "api_key"
    assert payload["environment"] == "production"
    assert KEY in credentials_file(cli).read_text()


def test_tty_menu_api_key_branch_targets_production_without_asking(cli):
    # Flagless means production on the key path exactly as on the browser
    # path — the menu never follows up with an environment question;
    # --env is how another target is named.
    cli.stderr_tty = True

    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--json", input="1\n" + KEY + "\n"
    )

    assert result.exit_code == 0, result.output
    assert "Environment" not in result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["environment"] == "production"
    creds = json.loads(credentials_file(cli).read_text())["environments"]
    assert creds["production"]["api_key"] == KEY


def test_tty_menu_env_flag_skips_the_environment_prompt(cli):
    cli.stderr_tty = True

    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--env", "eu", "--json", input="1\n" + KEY + "\n"
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["environment"] == "eu"
    assert "Environment" not in result.stderr  # the target was named by --env


def test_tty_menu_skipped_when_the_target_is_already_authenticated(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)  # production holds a credential
    cli.stderr_tty = True

    # No input scripted: a prompt would EOF-abort, so success proves the
    # stored credential short-circuited the acquire (ensure semantics).
    result = cli.invoke("auth", "login", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["already_authenticated"] is True
    assert "How would you like to log in?" not in result.stderr


def test_tty_menu_reprompts_on_an_unknown_choice(cli):
    cli.stderr_tty = True

    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--json", input="x\n1\n" + KEY + "\n"
    )

    assert result.exit_code == 0, result.output
    assert "Choose 1 or 2." in result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["method"] == "api_key"


def test_non_tty_login_without_key_falls_to_browser_and_names_the_key_remediation(cli):
    # ADR-0008: a non-terminal flagless login with no piped key goes
    # straight into the browser flow; where no browser can open, the
    # failure names the headless spellings (--api-key / ADE_API_KEY).
    result = cli.invoke("auth", "login", "--json", browser=lambda _url: False)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "no_browser"
    # --json emits only the payload, so the remediation must be in it.
    assert "--api-key" in payload["message"]
    assert "ADE_API_KEY" in payload["message"]


def test_headless_login_reads_a_key_piped_on_stdin(cli):
    """F2: `echo $KEY | ade auth login` is the headless spelling of the
    prompt — setup must not dead-end where automation lives."""
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--json", input=KEY + "\n")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["method"] == "api_key"
    assert payload["stored"] is True
    assert KEY not in result.stdout  # the payload masks it
    creds = json.loads(credentials_file(cli).read_text())["environments"]
    assert creds["production"]["api_key"] == KEY


def test_headless_login_honors_the_resolved_environment(cli):
    """A piped key files under the same target every other verb resolves
    (ADR-0003) — the pipe is a credential source, not a target."""
    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke(
        "auth", "login", "--env", "eu", "--json", input=KEY + "\n"
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["environment"] == "eu"
    creds = json.loads(credentials_file(cli).read_text())["environments"]
    assert set(creds) == {"eu"}


def test_headless_login_with_empty_stdin_still_names_the_remediation(cli):
    """An empty pipe is not a key: the browser fallback's failure names
    the headless spellings, and nothing blank is stored."""
    result = cli.invoke(
        "auth", "login", "--json", input="\n", browser=lambda _url: False
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "no_browser"
    assert "ADE_API_KEY" in payload["message"]
    assert not credentials_file(cli).exists()


def test_headless_login_remediation_names_the_pipe(cli):
    """The escape hatches are discoverable before the failure, but the
    error must still list all of them — including the piped form."""
    payload = json.loads(
        cli.invoke(
            "auth", "login", "--json", browser=lambda _url: False
        ).stdout
    )

    assert "echo $KEY | ade auth login" in payload["message"]
    assert "--api-key" in payload["message"]
    assert "ADE_API_KEY" in payload["message"]


def test_login_verifies_the_key_and_other_auth_commands_stay_offline(cli):
    # Login's single network call is the verification probe (#117);
    # status, logout, and every store-served command stay offline.
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)
    cli.invoke("auth", "status")
    cli.invoke("auth", "logout")
    cli.invoke("version")

    (probe,) = cli.transport.requests
    assert probe.method == "POST"
    assert probe.url == "https://api.ade.landing.ai/v2/telemetry"


# --- Environments coexist per invocation; nothing is selected (#87) ---


def test_flagless_login_targets_production_and_spares_other_envs(cli):
    # The issue #72 scenario, now structural: a bare login means production
    # and cannot overwrite the staging key (no sticky state exists).
    staging_key = "sk-staging-1111aaaa"
    prod_key = "sk-prod-2222bbbb"
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", staging_key, "--env", "staging")

    cli.transport.respond(200, {"accepted": 0})
    result = cli.invoke("auth", "login", "--api-key", prod_key, "--json")

    payload = json.loads(result.stdout)
    assert payload["environment"] == "production"
    creds = json.loads(credentials_file(cli).read_text())["environments"]
    assert creds["staging"]["api_key"] == staging_key  # untouched
    assert creds["production"]["api_key"] == prod_key


def test_login_env_with_a_stored_credential_is_a_no_op(cli):
    staging_key = "sk-staging-1111aaaa"
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", staging_key, "--env", "staging")

    result = cli.invoke("auth", "login", "--env", "staging", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["already_authenticated"] is True
    assert payload["environment"] == "staging"
    assert payload["credential"].endswith(staging_key[-4:])
    # Ensure means ensure: no config written, no prompts, and no network
    # beyond the first login's verification probe (#117).
    assert not config_file(cli).exists()
    assert len(cli.transport.requests) == 1


def test_logout_env_leaves_other_environments_authenticated(cli):
    staging_key = "sk-staging-1111aaaa"
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", staging_key, "--env", "staging")
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)  # active = production

    result = cli.invoke("auth", "logout", "--env", "staging", "--json")

    payload = json.loads(result.stdout)
    assert payload == {
        "logged_out": True,
        "cleared": True,
        "revoked": 0,
        "scope": "environment",
        "environment": "staging",
    }
    creds = json.loads(credentials_file(cli).read_text())["environments"]
    assert "staging" not in creds
    assert creds["production"]["api_key"] == KEY  # production survives
    # Logout never changes selection: still pointed at production.
    assert json.loads(cli.invoke("auth", "status", "--json").stdout)["environment"] == (
        "production"
    )


def test_logout_all_clears_every_environment(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", "sk-staging-1111aaaa", "--env", "staging")
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY, "--env", "dev")

    result = cli.invoke("auth", "logout", "--all", "--json")

    payload = json.loads(result.stdout)
    assert payload["scope"] == "all"
    assert payload["environment"] is None
    assert not credentials_file(cli).exists()


def test_logout_rejects_env_and_all_together(cli):
    result = cli.invoke("auth", "logout", "--env", "staging", "--all", "--json")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "ambiguous_target"


def test_status_lists_other_authenticated_environments(cli):
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", "sk-staging-1111aaaa", "--env", "staging")
    cli.transport.respond(200, {"accepted": 0})
    cli.invoke("auth", "login", "--api-key", KEY)  # active = production

    result = cli.invoke("auth", "status", "--json")

    others = json.loads(result.stdout)["other_environments"]
    assert others == [{"environment": "staging", "method": "api_key"}]
    human = cli.invoke("auth", "status").stdout
    assert "Also authenticated: staging (api_key)" in human
