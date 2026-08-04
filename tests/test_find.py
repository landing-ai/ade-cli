"""``find`` and the elements projection — driven through the CLI seam.

The projection is written at parse finalize and is recomputable from the
raw response alone; ``find`` is pure local filtering over it — zero API
calls, document order, never ranked.
"""

import json

import pytest

from parse_fixtures import rich_parse_response, completed_job

KEY = "sk-test-0123456789abcd"
AUTH_ENV = {"ADE_API_KEY": KEY}


def parse_file(cli, path, *, job_id="job-0001", data=None):
    """Seed the store: run a real parse to completion through the seam."""
    cli.transport.respond(202, {"job_id": job_id})
    cli.transport.respond(200, completed_job(data, job_id=job_id))
    result = cli.invoke("parse", "-d", str(path), "--json", env=AUTH_ENV)
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)["job_item_id"]


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 fake invoice bytes")
    return path


@pytest.fixture
def rich(cli, document):
    """A parsed two-page doc (text, table + cells, figure) and its raw data."""
    data = rich_parse_response()
    item_id = parse_file(cli, document, data=data)
    return item_id, data


def find_json(cli, *args):
    result = cli.invoke("find", *args, "--json")
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


DOCUMENT_ORDER = [
    "text-0",
    "table-0",
    "table_cell-0",
    "table_cell-1",
    "table_cell-2",
    "table_cell-3",
    "text-1",
    "figure-0",
]


# --- the projection artifact ---


def test_parse_finalize_writes_the_elements_projection(cli, rich):
    item_id, data = rich

    stored = json.loads((cli.home / "jobs" / item_id / "elements.json").read_text())

    assert stored["job_id"] == "job-0001"  # generation stamp
    records = stored["elements"]
    # Document order: page, then reading order; a table then its cells.
    assert [r["id"] for r in records] == DOCUMENT_ORDER
    # Every record's text is exactly its span sliced from the raw markdown —
    # the projection stays recomputable from the raw response alone.
    for record in records:
        start, end = record["span"]
        assert record["text"] == data["markdown"][start:end]
    meta = json.loads((cli.home / "jobs" / item_id / "meta.json").read_text())
    assert "elements.json" in meta["artifacts"]


def test_projection_cells_carry_row_col_and_tables_their_cells(cli, rich):
    item_id, data = rich

    stored = json.loads((cli.home / "jobs" / item_id / "elements.json").read_text())
    by_id = {r["id"]: r for r in stored["elements"]}

    cell = by_id["table_cell-3"]
    assert cell["text"] == "€21"
    assert (cell["row"], cell["col"]) == (1, 1)
    assert (cell["colspan"], cell["rowspan"]) == (1, 1)
    assert by_id["table-0"]["cells"] == [
        "table_cell-0",
        "table_cell-1",
        "table_cell-2",
        "table_cell-3",
    ]
    # Leaf elements keep their fine-grained grounding; a cell's is empty by
    # contract (no finer granularity than itself), a table carries none.
    assert by_id["text-0"]["atomic_grounding"] == [
        data["structure"]["children"][0]["children"][0]["grounding"]
    ]
    assert cell["atomic_grounding"] == []
    assert "atomic_grounding" not in by_id["table-0"]


# --- find: query & filters ---


def test_find_substring_is_case_insensitive_and_returns_citation_records(cli, rich):
    item_id, data = rich
    seen = len(cli.transport.requests)

    matches = find_json(cli, "--job", item_id, "TOTAL")

    text_1 = data["structure"]["children"][1]["children"][0]
    assert matches == [
        {
            "job_item_id": item_id,
            "element_id": "text-1",
            "type": "text",
            "page": 2,
            "box": text_1["grounding"]["box"],
            "text": "Total: €42\n\n",
        }
    ]
    assert len(cli.transport.requests) == seen  # read model: zero API calls


def test_find_returns_matches_in_document_order_never_ranked(cli, rich):
    item_id, _ = rich

    matches = find_json(cli, "--job", item_id, "2")

    # "2" hits the table's own markdown, two cells, and the total — returned
    # exactly in document order, not by any match-quality notion.
    assert [m["element_id"] for m in matches] == [
        "table-0",
        "table_cell-2",
        "table_cell-3",
        "text-1",
    ]


def test_find_without_query_lists_all_elements(cli, rich):
    item_id, _ = rich

    matches = find_json(cli, "--job", item_id)

    assert [m["element_id"] for m in matches] == DOCUMENT_ORDER


def test_find_filters_compose(cli, rich):
    item_id, _ = rich

    matches = find_json(
        cli, "--job", item_id, "2", "--type", "table_cell", "--page", "1"
    )
    assert [m["element_id"] for m in matches] == ["table_cell-2", "table_cell-3"]

    limited = find_json(
        cli, "--job", item_id, "2", "--type", "table_cell", "--limit", "1"
    )
    assert [m["element_id"] for m in limited] == ["table_cell-2"]


def test_find_regex(cli, rich):
    item_id, _ = rich

    matches = find_json(cli, "--job", item_id, "--regex", r"^total: €\d+")

    assert [m["element_id"] for m in matches] == ["text-1"]


def test_find_invalid_regex_is_a_usage_error(cli, rich):
    item_id, _ = rich

    result = cli.invoke("find", "--job", item_id, "--regex", "(", "--json")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "bad_query"


def test_find_regex_flag_requires_a_query(cli, rich):
    item_id, _ = rich

    result = cli.invoke("find", "--job", item_id, "--regex", "--json")

    assert result.exit_code == 2


def test_find_element_id_resolves_known_ids(cli, rich):
    item_id, _ = rich

    matches = find_json(
        cli, "--job", item_id,
        "--element-id", "figure-0", "--element-id", "table_cell-0",
    )

    # Still document order, whatever order the ids were asked in.
    assert [m["element_id"] for m in matches] == ["table_cell-0", "figure-0"]


def test_find_type_filter_makes_citing_a_cell_mechanical(cli, rich):
    item_id, _ = rich

    matches = find_json(cli, "--job", item_id, "€21", "--type", "table_cell")

    assert [m["element_id"] for m in matches] == ["table_cell-3"]


# --- find: multi-item ---


def test_find_multi_item_tags_every_match_with_its_job_item_id(cli, tmp_path, document):
    item_a = parse_file(cli, document, data=rich_parse_response())
    other = tmp_path / "receipt.pdf"
    other.write_bytes(b"%PDF-1.4 other bytes")
    item_b = parse_file(cli, other, job_id="job-0002",
                       data=rich_parse_response(job_id="job-0002"))

    matches = find_json(cli, "--job", item_b, "--job", item_a, "invoice")

    # Items come back in the order their ids were given, each match tagged.
    assert [(m["job_item_id"], m["element_id"]) for m in matches] == [
        (item_b, "text-0"),
        (item_a, "text-0"),
    ]


def test_find_repeated_id_to_the_same_item_is_one_result_set(cli, rich):
    item_id, _ = rich

    # The full id and an unambiguous prefix resolve to the same item — one
    # result set, not two.
    matches = find_json(cli, "--job", item_id, "--job", item_id[:8], "TOTAL")

    assert [m["element_id"] for m in matches] == ["text-1"]


# --- find: errors & recomputability ---


def test_find_takes_the_job_id_positionally_like_every_other_command(cli, rich):
    # One calling convention across the surface (feedback F1): the id is
    # positional here too; --job stays as the multi-item spelling.
    item_id, _ = rich

    assert find_json(cli, item_id, "TOTAL") == find_json(cli, "--job", item_id, "TOTAL")
    # A prefix resolves, and no query still lists every element.
    assert [m["element_id"] for m in find_json(cli, item_id[:8])] == DOCUMENT_ORDER


def test_find_with_job_flag_treats_the_positional_as_the_query(cli, rich):
    item_id, _ = rich

    # `--job ID "total"` is the long-standing spelling; unchanged.
    matches = find_json(cli, "--job", item_id, "total")
    assert [m["element_id"] for m in matches] == ["text-1"]

    # But two positionals alongside --job is ambiguous — refused, named.
    result = cli.invoke("find", item_id, "total", "--job", item_id, "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "bad_query"


def test_find_requires_a_job_id(cli):
    result = cli.invoke("find", "--json")

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "bad_query"
    assert "JOB_ITEM_ID" in payload["message"]
    assert "history list" in payload["message"]


def test_find_bare_token_is_read_as_a_job_id(cli):
    # `find total` used to be "query without --job"; the token is now the
    # JOB_ID slot, so an unknown one errors as an id, not as a bad query.
    result = cli.invoke("find", "total", "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "unknown_id"


def test_find_unknown_id_errors(cli):
    result = cli.invoke("find", "--job", "no-such-item", "x", "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "unknown_id"


def test_find_on_an_extract_item_names_the_parse_to_search(cli, rich, tmp_path):
    from extract_fixtures import SCHEMA, completed_extract_job, extract_result

    parse_id, data = rich
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200, completed_extract_job(extract_result(markdown=data["markdown"]))
    )
    extracted = cli.invoke(
        "extract", parse_id, "--schema", str(schema), "--json", env=AUTH_ENV
    )
    assert extracted.exit_code == 0, extracted.stdout
    extract_id = json.loads(extracted.stdout)["job_item_id"]

    result = cli.invoke("find", "--job", extract_id, "total", "--json")

    # find searches parse items only; the error hands over the parse id
    # instead of dead-ending at `run ade parse first`.
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_a_parse_item"
    assert payload["parse_item_id"] == parse_id
    assert f"--job {parse_id}" in payload["message"]

    human = cli.invoke("find", "--job", extract_id, "total")
    assert f"--job {parse_id}" in human.stdout


def test_find_on_extract_whose_parse_was_deleted_names_the_deletion(
    cli, rich, tmp_path
):
    import shutil

    from extract_fixtures import SCHEMA, completed_extract_job, extract_result

    parse_id, data = rich
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200, completed_extract_job(extract_result(markdown=data["markdown"]))
    )
    extracted = cli.invoke(
        "extract", parse_id, "--schema", str(schema), "--json", env=AUTH_ENV
    )
    assert extracted.exit_code == 0, extracted.stdout
    extract_id = json.loads(extracted.stdout)["job_item_id"]
    shutil.rmtree(cli.home / "jobs" / parse_id)

    result = cli.invoke("find", "--job", extract_id, "total", "--json")

    # The deleted parse is named (crop's no_parse_linkage diagnosis) —
    # never a bare "no parse to search", never a `--job` suggestion
    # pointing at a missing item.
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_a_parse_item"
    assert payload["parse_item_id"] == parse_id
    assert "deleted" in payload["message"]
    assert "--job" not in payload["message"]


def test_find_on_a_markdown_extract_item_has_no_parse_to_search(cli, tmp_path):
    from extract_fixtures import SCHEMA, completed_extract_job, extract_result

    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nTotal: €42\n")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    cli.transport.respond(202, {"job_id": "extract-0001"})
    cli.transport.respond(
        200, completed_extract_job(extract_result(markdown=md.read_text()))
    )
    extracted = cli.invoke(
        "extract", "--markdown", str(md), "--schema", str(schema),
        "--json", env=AUTH_ENV,
    )
    assert extracted.exit_code == 0, extracted.stdout
    extract_id = json.loads(extracted.stdout)["job_item_id"]

    result = cli.invoke("find", "--job", extract_id, "total", "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_a_parse_item"
    assert "parse_item_id" not in payload
    assert "no parse to search" in payload["message"]


def test_find_unparsed_doc_is_an_error(cli, document):
    cli.transport.respond(202, {"job_id": "job-0001"})
    result = cli.invoke(
        "parse", "-d", str(document), "--wait", "0", "--json", env=AUTH_ENV
    )
    assert result.exit_code == 3  # pending: submitted, not parsed
    item_id = json.loads(result.stdout)["job_item_id"]

    result = cli.invoke("find", "--job", item_id, "total", "--json")

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "not_parsed"


def test_find_recomputes_from_the_raw_response_when_projection_is_missing(cli, rich):
    item_id, _ = rich
    projection = cli.home / "jobs" / item_id / "elements.json"
    projection.unlink()

    matches = find_json(cli, "--job", item_id, "TOTAL")

    assert [m["element_id"] for m in matches] == ["text-1"]
    assert not projection.exists()  # find is a read model; it writes nothing


def test_find_never_serves_a_projection_from_another_generation(cli, rich):
    item_id, _ = rich
    projection = cli.home / "jobs" / item_id / "elements.json"
    stale = json.loads(projection.read_text())
    stale["job_id"] = "job-older"
    for record in stale["elements"]:
        record["text"] = "TAMPERED"
    projection.write_text(json.dumps(stale))

    matches = find_json(cli, "--job", item_id, "TOTAL")

    # The stamp mismatch routes around the stale file to the raw response.
    assert [m["element_id"] for m in matches] == ["text-1"]
    assert matches[0]["text"] == "Total: €42\n\n"


def test_find_no_matches_is_an_empty_array(cli, rich):
    item_id, _ = rich

    result = cli.invoke("find", "--job", item_id, "zebra", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_find_human_output_lists_one_line_per_match(cli, rich):
    item_id, _ = rich

    result = cli.invoke("find", "--job", item_id, "total")

    assert result.exit_code == 0
    assert "text-1" in result.stdout
    assert item_id in result.stdout
    assert "Total: €42" in result.stdout
