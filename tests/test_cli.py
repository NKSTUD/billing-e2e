import pytest

from billing_e2e.cli import main


def test_help_lists_the_commands(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["--help"])
    assert exit.value.code == 0
    out = capsys.readouterr().out
    assert all(command in out for command in ("run", "list", "init"))
