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


def test_setup_file_is_idempotent(live_server, tmp_path, capsys):
    import json

    common = ["--url", live_server, "--token", ADMIN]
    setup = {
        "settings": {
            "approvers": [{"name": "Jo Bloggs", "slack_user_id": "U0AAAAAAAA2"}],
            "review_channel": {"id": "C0REVIEW01", "name": "#pacer-base-review"},
            "agency_channels": [{"id": "C0ALL00001", "name": "#pacer-all"}],
        },
        "clients": [{
            "name": "Acme", "slug": "acme", "aliases": ["AC"], "domains": ["acme.example"],
            "sources": [{"system": "slack", "ref": "C0INT00001", "name": "#internal-acme", "role": "internal"}],
        }],
    }
    path = tmp_path / "setup.json"
    path.write_text(json.dumps(setup))
    assert main(["setup", str(path), *common]) == 0
    out = capsys.readouterr().out
    assert "client acme: created" in out and "1 source(s) set" in out

    setup["clients"][0]["aliases"] = ["AC", "Acme Ltd"]
    path.write_text(json.dumps(setup))
    assert main(["setup", str(path), *common]) == 0
    assert "already there, details updated" in capsys.readouterr().out

    assert main(["sources", "acme", *common]) == 0
    assert "#internal-acme" in capsys.readouterr().out
    assert main(["settings", *common]) == 0
    assert "#pacer-base-review" in capsys.readouterr().out
    assert main(["clients", *common]) == 0
    assert "acme" in capsys.readouterr().out
    assert main(["proposals", "acme", *common]) == 0
    assert "No pending proposals" in capsys.readouterr().out
