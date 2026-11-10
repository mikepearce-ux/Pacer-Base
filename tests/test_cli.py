import pytest

from conftest import ADMIN
from pacer_base.cli import main


def test_admin_commands_against_a_live_server(live_server, capsys):
    common = ["--url", live_server, "--token", ADMIN]
    assert main(["add-client", "S&S", "--alias", "SNS", "--domain", "sns.example", *common]) == 0
    assert "Created S&S as 's-and-s'" in capsys.readouterr().out

    assert main(["add-token", "Team connector", "--scope", "write", "--client", "s-and-s", *common]) == 0
    out = capsys.readouterr().out
    assert f"{live_server}/mcp/pb_" in out and "claude mcp add --transport http pacer-base" in out

    assert main(["clients", *common]) == 0
    assert "s-and-s" in capsys.readouterr().out
    assert main(["tokens", *common]) == 0
    listing = capsys.readouterr().out
    assert "Team connector" in listing and "pb_" not in listing

    assert main(["update-client", "s-and-s", "--status", "paused", *common]) == 0
    assert '"status": "paused"' in capsys.readouterr().out
    assert main(["context", "s-and-s", *common]) == 0
    assert "# S&S: Pacer Base context" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="409"):
        main(["add-client", "S&S", *common])
    assert main(["revoke-token", "1", *common]) == 0
    with pytest.raises(SystemExit, match="404"):
        main(["revoke-token", "1", *common])


def test_needs_url_and_token(monkeypatch):
    monkeypatch.delenv("PACER_BASE_URL", raising=False)
    monkeypatch.delenv("PACER_BASE_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="PACER_BASE_URL"):
        main(["clients"])
