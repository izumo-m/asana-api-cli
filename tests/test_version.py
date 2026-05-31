from importlib.metadata import version

from asana_api_cli.version import version_string


def test_version_string_format():
    cli_ver = version("asana-api-cli")
    sdk_ver = version("asana")
    click_ver = version("click")
    result = version_string()
    assert result == f"{cli_ver} (python-asana {sdk_ver}, click {click_ver})"


def test_version_cli(tmp_path):
    from _cli_runner import full_output, make_runner

    from asana_api_cli.cli import main

    runner = make_runner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    out = full_output(result)
    assert "asana-api" in out
    assert "python-asana" in out
    assert "click" in out
