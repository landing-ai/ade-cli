"""Live integration suite: the release gate against production.

Unlike the offline suite (in-process runner, faked transport), every test
here drives the CLI as a real subprocess against the production ADE
service — auth verification, parse and extract submits/polls, then the
local verbs (find, crop) over what production returned. Run by
.github/workflows/integration.yml on macOS and Windows; the release
pipeline must pass it before anything is tagged or published (issue #175).

Gated on ``ADE_INTEGRATION_API_KEY``: a production API key. Without it
every test skips, so plain ``pytest`` stays hermetic and free. To run
locally::

    ADE_INTEGRATION_API_KEY=<key> uv run pytest tests/integration -v

The suite is one workflow in file order — login once, parse the fixture
once (session fixtures), then each verb asserts against that shared job
item. The fixture PDF's text is fixed (see FIXTURE_LINES), so content
assertions are exact, not model-lenient — except extraction values,
where only the trivially-quoted invoice number is asserted.

Parse and extract bill real credits (one parse + one extract per run,
per OS). Everything else is served from the local store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

API_KEY = os.environ.get("ADE_INTEGRATION_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not API_KEY, reason="ADE_INTEGRATION_API_KEY not set (live production suite)"
)

FIXTURE = Path(__file__).parent / "fixtures" / "invoice.pdf"
# The text baked into fixtures/invoice.pdf (regenerate: see the docstring
# there is none — the PDF is hand-assembled; keep these in sync with it).
FIXTURE_LINES = [
    "ADE CLI Integration Fixture",
    "Invoice Number: INV-2026-0806",
    "Customer: Example Corp",
    "Total Due: 123.45 USD",
]
INVOICE_NUMBER = "INV-2026-0806"

# Generous per-command ceiling: parse/extract poll server-side runs (the
# CLI's own --wait default is 600s). A hang, not slowness, is the failure
# this guards.
COMMAND_TIMEOUT = 900.0


def run_ade(
    args: list[str],
    home: Path,
    *,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real CLI out of process, homed at a temp store, targeting
    production. Every ADE_* variable is dropped so the run can't inherit a
    developer's ADE_API_KEY/ADE_ENV/ADE_ENDPOINT — the stored credential
    written by the login test is the only auth in play."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ADE_")}
    env["ADE_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "ade_cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        input=stdin,
        timeout=COMMAND_TIMEOUT,
    )


def payload_of(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, (
        f"exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="session")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ade-integration-home")


@pytest.fixture(scope="session")
def logged_in(home: Path) -> Path:
    """Login once for the whole suite. `--api-key -` reads the key from
    piped stdin — the headless path agents use, and the one that broke on
    Windows in v1.0.2 — and verifies it live against production before
    storing (ADR-0007)."""
    result = run_ade(
        ["login", "--api-key", "-", "--json"], home, stdin=API_KEY + "\n"
    )
    payload = payload_of(result)
    assert payload["verified"] is True  # ADR-0007: verified live before storing
    assert payload["stored"] is True
    assert payload["environment"] == "production"
    return home


@pytest.fixture(scope="session")
def parsed(logged_in: Path) -> dict:
    """One real parse of the fixture; every downstream verb reads it."""
    return payload_of(
        run_ade(["parse", "-d", str(FIXTURE), "--json"], logged_in)
    )


def test_auth_status_reports_the_stored_key(logged_in: Path) -> None:
    payload = payload_of(run_ade(["auth", "status", "--json"], logged_in))
    assert payload["authenticated"] is True
    assert payload["method"] == "api_key"
    assert payload["source"] == "stored"
    assert payload["environment"] == "production"
    assert payload["credential"].endswith(API_KEY[-4:])


def test_login_rejects_an_invalid_key(logged_in: Path, tmp_path: Path) -> None:
    """Auth isn't just the happy path: production's 401 must come back as
    the one canonical invalid-key error, and nothing may be stored. A
    fresh home so the real credential is never at risk."""
    bad_home = tmp_path / "bad-home"
    result = run_ade(
        ["login", "--api-key", "-", "--json"], bad_home, stdin="sk-not-a-real-key\n"
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["error"] == "invalid_api_key"
    assert payload["status_code"] == 401
    assert not (bad_home / "credentials.json").exists()


def test_parse_completes_against_production(parsed: dict) -> None:
    assert parsed["status"] == "parsed"
    assert parsed["environment"] == "production"
    assert parsed["page_count"] == 1
    assert parsed["failed_pages"] == []
    assert parsed["job_item_id"]
    assert parsed["run_id"]
    markdown = Path(parsed["store_dir"], "parse.md").read_text(encoding="utf-8")
    assert INVOICE_NUMBER in markdown


def test_parse_rerun_is_served_from_the_store(parsed: dict, logged_in: Path) -> None:
    """The guarantee contract: the same invocation dedups to the same job
    item and bills nothing the second time."""
    again = payload_of(run_ade(["parse", "-d", str(FIXTURE), "--json"], logged_in))
    assert again["job_item_id"] == parsed["job_item_id"]
    assert again["cached"] is True
    # The same server-side run — nothing was resubmitted, so nothing new
    # billed. (`credits` echoes what the original run billed, so it is no
    # signal here.)
    assert again["run_id"] == parsed["run_id"]


def test_find_searches_the_parsed_elements(parsed: dict, logged_in: Path) -> None:
    matches = payload_of(
        run_ade(
            ["find", parsed["job_item_id"], "invoice number", "--json"], logged_in
        )
    )
    assert isinstance(matches, list) and matches
    hit = matches[0]
    assert hit["job_item_id"] == parsed["job_item_id"]
    assert hit["page"] == 1
    assert INVOICE_NUMBER in hit["text"]
    box = hit["box"]
    assert 0 <= box["xmin"] < box["xmax"] <= 1
    assert 0 <= box["ymin"] < box["ymax"] <= 1


def test_crop_renders_every_element_to_png(
    parsed: dict, logged_in: Path, tmp_path: Path
) -> None:
    out = tmp_path / "crops"
    payload = payload_of(
        run_ade(
            [
                "crop",
                parsed["job_item_id"],
                "--all",
                "--output",
                str(out),
                "--json",
            ],
            logged_in,
        )
    )
    assert payload["status"] == "cropped"
    assert payload["count"] == len(payload["crops"]) > 0
    for crop in payload["crops"]:
        png = Path(crop["path"])
        assert png.is_file()
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert crop["width"] > 0 and crop["height"] > 0


def test_extract_pulls_the_invoice_number(parsed: dict, logged_in: Path) -> None:
    schema = json.dumps(
        {
            "type": "object",
            "properties": {
                "invoice_number": {
                    "type": "string",
                    "description": "The invoice number, verbatim.",
                }
            },
            "required": ["invoice_number"],
        }
    )
    payload = payload_of(
        run_ade(
            ["extract", parsed["job_item_id"], "--schema", schema, "--json"],
            logged_in,
        )
    )
    assert payload["status"] == "extracted"
    assert payload["environment"] == "production"
    assert payload["parse_job_item_id"] == parsed["job_item_id"]
    assert payload["job_item_id"] != parsed["job_item_id"]
    assert payload["extraction"]["invoice_number"] == INVOICE_NUMBER


def test_logout_clears_the_credential(logged_in: Path) -> None:
    """Last in file order — every earlier test rides the stored login."""
    payload = payload_of(run_ade(["logout", "--json"], logged_in))
    assert payload["cleared"] is True
    assert payload["environment"] == "production"
    status = run_ade(["auth", "status", "--json"], logged_in)
    assert status.returncode == 1  # unauthenticated is a distinct exit state
    assert json.loads(status.stdout)["authenticated"] is False
