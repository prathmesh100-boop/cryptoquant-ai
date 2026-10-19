import json

from app.main import main


def test_check_config_runs_and_never_prints_secrets(monkeypatch, capsys):
    monkeypatch.setenv("CQ_EXCHANGE__API_KEY", "super-secret-key")
    monkeypatch.setenv("CQ_EXCHANGE__API_SECRET", "super-secret-value")
    assert main(["--check-config"]) == 0
    err = capsys.readouterr().err
    assert "super-secret" not in err
    rec = json.loads(err.strip().splitlines()[-1])
    assert rec["mode"] == "PAPER" and rec["real_orders_possible"] is False and rec["auth_configured"] is True


def test_invalid_config_exits_cleanly_without_echoing_values(monkeypatch, capsys):
    monkeypatch.setenv("CQ_MODE", "LIVE")
    monkeypatch.setenv("CQ_EXCHANGE__API_KEY", "leaky-key-value")
    assert main(["--check-config"]) == 2
    err = capsys.readouterr().err
    assert "Invalid configuration" in err and "leaky-key-value" not in err
