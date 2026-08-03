import json
from importlib.metadata import version as installed_version


def test_version_prints_the_installed_version(cli):
    result = cli.invoke("version")

    assert result.exit_code == 0
    assert installed_version("ade-cli") in result.stdout


def test_version_json_emits_one_stable_object(cli):
    result = cli.invoke("version", "--json")

    assert result.exit_code == 0
    # The install mode rides along (#138): the test runner is never the
    # frozen binary, so the mode here is always "python".
    assert json.loads(result.stdout) == {
        "version": installed_version("ade-cli"),
        "install": "python",
    }
