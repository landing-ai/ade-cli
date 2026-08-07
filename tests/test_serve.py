"""``view --serve`` — the loopback door onto the viewer artifacts
(ADR-0005).

The CLI-seam tests script the health probe through the injected transport
(the same seam every network verb uses) and fake the daemon spawn; the
daemon's own handler is exercised for real on an OS-assigned loopback
port — the one place a socket is honest and still offline.
"""

import json
import threading

import httpx
import pytest

from test_view import seed_parse_item, view_json

from ade_cli import serve


@pytest.fixture
def parsed_url_item(cli):
    """A parse item with an http source: no local imagery to render, so
    the full view flow runs without pypdfium2 work."""
    return seed_parse_item(cli, url="https://docs.example.com/manual.pdf")


def _health_body(cli):
    return {"ade": True, "home": str(cli.home)}


def _record_server(cli, port, pid=4242):
    cli.home.mkdir(parents=True, exist_ok=True)
    (cli.home / serve.STATE_FILE).write_text(json.dumps({"port": port, "pid": pid}))


def test_serve_reuses_a_running_server(cli, parsed_url_item, monkeypatch):
    item_id = parsed_url_item
    _record_server(cli, 8642)
    cli.transport.respond(200, _health_body(cli))
    monkeypatch.setattr(
        "ade_cli.view._spawn_server",
        lambda port: pytest.fail("a live server must be reused, not respawned"),
    )
    payload = view_json(cli, item_id, "--serve", "--no-download")
    assert payload["url"] == f"http://127.0.0.1:8642/jobs/{item_id}/view.html"
    assert payload["serve_error"] is None
    probe = cli.transport.requests[-1]
    assert str(probe.url) == "http://127.0.0.1:8642/__ade__/health"


def test_serve_spawns_the_daemon_and_waits_for_health(
    cli, parsed_url_item, monkeypatch
):
    item_id = parsed_url_item
    spawned = []

    def fake_spawn(port):
        # The daemon's observable effects: server.json records the final
        # port (here: a bind race moved it off the candidate) and health
        # starts answering.
        spawned.append(port)
        _record_server(cli, 8644)
        cli.transport.respond(200, _health_body(cli))

    monkeypatch.setattr("ade_cli.view._spawn_server", fake_spawn)
    payload = view_json(cli, item_id, "--serve", "--no-download")
    assert spawned == [serve.DEFAULT_PORT]
    assert payload["url"] == f"http://127.0.0.1:8644/jobs/{item_id}/view.html"
    assert payload["deep_link"] is None


def test_serve_ignores_a_server_over_another_store(
    cli, parsed_url_item, monkeypatch
):
    """A recorded port answering for a DIFFERENT ADE_HOME is not ours:
    the health body names the store root, and a mismatch respawns."""
    item_id = parsed_url_item
    _record_server(cli, 8642)
    cli.transport.respond(200, {"ade": True, "home": "/somewhere/else"})

    def fake_spawn(port):
        _record_server(cli, 8645)
        cli.transport.respond(200, _health_body(cli))

    monkeypatch.setattr("ade_cli.view._spawn_server", fake_spawn)
    payload = view_json(cli, item_id, "--serve", "--no-download")
    assert payload["url"] == f"http://127.0.0.1:8645/jobs/{item_id}/view.html"


def test_serve_failure_degrades_to_file(cli, parsed_url_item, monkeypatch):
    """A daemon that never comes up must not fail the command: the
    artifact on disk is complete, so the file:// door opens with a note."""
    item_id = parsed_url_item
    monkeypatch.setattr("ade_cli.view._spawn_server", lambda port: None)
    opened = []
    result = cli.invoke(
        "view", item_id, "--serve", "--open", "--no-sidebar-sync", "--no-download",
        "--json",
        browser=lambda url: opened.append(url) or True,
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["url"] is None
    assert "did not come up" in payload["serve_error"]
    assert opened and opened[0].startswith("file://")


def test_serve_degrades_when_the_spawn_itself_fails(
    cli, parsed_url_item, monkeypatch
):
    """A failed re-exec (Popen raising) is a serve failure like any other:
    file:// door with a note, never a traceback."""
    item_id = parsed_url_item

    def broken_spawn(port):
        raise OSError("posix_spawn failed")

    monkeypatch.setattr("ade_cli.view._spawn_server", broken_spawn)
    payload = view_json(cli, item_id, "--serve", "--no-download")
    assert payload["url"] is None
    assert "could not start the viewer server" in payload["serve_error"]
    assert payload["path"].endswith("view.html")


def test_serve_url_is_the_browser_target_with_deep_link(
    cli, parsed_url_item, monkeypatch
):
    item_id = parsed_url_item
    _record_server(cli, 8642)
    cli.transport.respond(200, _health_body(cli))
    monkeypatch.setattr("ade_cli.view._spawn_server", lambda port: None)
    opened = []
    result = cli.invoke(
        "view", item_id, "--serve", "--open", "--element-id", "text-0",
        "--no-download",
        "--no-sidebar-sync", "--json",
        browser=lambda url: opened.append(url) or True,
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert opened == [
        f"http://127.0.0.1:8642/jobs/{item_id}/view.html#element=text-0"
    ]
    assert payload["deep_link"] == opened[0]


def test_daemon_serves_artifacts_health_and_refuses_listings(cli, parsed_url_item):
    """The real handler on a real loopback socket: health names the store,
    artifacts stream, directory listings are refused (ADR-0005)."""
    item_id = parsed_url_item
    view_json(cli, item_id, "--no-download")  # build what the server serves
    server = serve._bind(cli.home, 0)  # candidate 0 = OS-assigned, race-free
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        health = httpx.get(f"{base}{serve.HEALTH_PATH}")
        assert health.status_code == 200
        assert health.json() == {"ade": True, "home": str(cli.home)}

        page = httpx.get(f"{base}/jobs/{item_id}/view.html")
        assert page.status_code == 200
        assert "__adeDbg" not in page.text  # debug tracer must not ship
        # Rebuilt-in-place artifacts must never serve stale from http
        # cache: every response demands revalidation (304 keeps it fast).
        assert page.headers["Cache-Control"] == "no-cache"

        assert httpx.get(f"{base}/jobs/").status_code == 403

        # The store root is secret to the CLI: only jobs/ and history.js
        # are reachable — credentials.json by exact path must 404, and a
        # dot-smuggled prefix ("/jobs/%2e%2e/...") collapses before the
        # allowlist check. history.js was written by the build above.
        (cli.home / "credentials.json").write_text('{"environments": {}}')
        assert httpx.get(f"{base}/credentials.json").status_code == 404
        assert httpx.get(f"{base}/jobs/%2e%2e/credentials.json").status_code == 404
        assert httpx.get(f"{base}/config.json").status_code == 404
        assert httpx.get(f"{base}/history.js").status_code == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_stop_server_asks_a_live_server_to_retire(cli, monkeypatch):
    _record_server(cli, 8642)
    cli.transport.respond(200, _health_body(cli))  # the ours? probe
    cli.transport.respond(200)  # the shutdown POST
    result = cli.invoke("view", "--stop-server", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"status": "server_stopped", "port": 8642}
    shutdown = cli.transport.requests[-1]
    assert shutdown.method == "POST"
    assert str(shutdown.url) == "http://127.0.0.1:8642/__ade__/shutdown"


def test_stop_server_falls_back_to_the_pid_for_pre_endpoint_daemons(
    cli, monkeypatch
):
    """A daemon from an older build answers health but 501s the shutdown
    POST — the recorded pid (just proven live and ours by the probe)
    gets SIGTERM instead, so upgrades never strand a server."""
    _record_server(cli, 8642, pid=54321)
    cli.transport.respond(200, _health_body(cli))
    cli.transport.respond(501)
    killed = []
    monkeypatch.setattr(
        "os.kill", lambda pid, sig: killed.append((pid, sig))
    )
    result = cli.invoke("view", "--stop-server", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"status": "server_stopped", "port": 8642}
    import signal

    assert killed == [(54321, signal.SIGTERM)]


def test_stop_server_never_signals_a_pid_when_the_post_errors(cli, monkeypatch):
    """A shutdown POST that errors (timeout, reset) is ambiguous — the
    recorded pid is only as trustworthy as the daemon that wrote it, so
    the command reports remediation instead of signaling blind."""
    _record_server(cli, 8642, pid=54321)
    cli.transport.respond(200, _health_body(cli))
    cli.transport.respond_with(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("reset"))
    )
    monkeypatch.setattr(
        "os.kill", lambda pid, sig: pytest.fail("must not signal on an errored POST")
    )
    result = cli.invoke("view", "--stop-server", "--json")
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["error"] == "server_stop_failed"
    assert "kill 54321" in payload["message"]


def test_stop_server_reports_when_nothing_is_running(cli):
    result = cli.invoke("view", "--stop-server", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"status": "server_not_running", "port": None}


def test_daemon_shutdown_endpoint_and_sticky_state(cli, parsed_url_item):
    """POST /__ade__/shutdown drains the server; server.json survives so
    the port stays sticky for the next spawn (ADR-0005)."""
    _record_server(cli, 9999)  # pre-existing state must survive shutdown
    server = serve._bind(cli.home, 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert (
            httpx.post(f"http://127.0.0.1:{port}{serve.SHUTDOWN_PATH}").status_code
            == 200
        )
        thread.join(timeout=5)
        assert not thread.is_alive(), "shutdown endpoint must drain serve_forever"
        assert json.loads((cli.home / serve.STATE_FILE).read_text())["port"] == 9999
    finally:
        server.server_close()


def test_daemon_retires_after_idle_timeout(cli):
    """The watchdog ends serve_forever once no request lands within the
    window — requests (any open viewer's 3s poll) push it out."""
    import time

    activity = {"last": time.monotonic()}
    server = serve._bind(cli.home, 0, activity)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        watchdog = threading.Thread(
            target=serve._watch_idle, args=(server, activity, 0.3, 0.05), daemon=True
        )
        watchdog.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "idle watchdog must retire the server"
    finally:
        server.server_close()


def test_template_gates_zoom_interception_to_file_urls():
    """Static invariant (ADR-0005): the CSS-zoom approximation is file://
    compatibility code — served pages leave zoom to the browser."""
    from importlib import resources

    template = (
        resources.files("ade_cli").joinpath("view_template.html").read_text("utf-8")
    )
    assert 'location.protocol === "file:"' in template
    assert "zoomSupported && FILE_MODE" in template
    assert "__adeDbg" not in template
