import pytest
from pydantic import ValidationError

from app.config import ExecutionMode, Settings

CREDS = {"CQ_EXCHANGE__API_KEY": "k-test", "CQ_EXCHANGE__API_SECRET": "s-test"}


def test_defaults_are_safe():
    s = Settings()
    assert s.mode is ExecutionMode.PAPER
    assert s.live_trading_confirmed is False
    assert s.allows_real_orders is False
    assert s.risk.risk_per_trade_pct == 0.5
    assert s.risk.daily_loss_limit_pct == 2.0
    assert s.risk.weekly_loss_limit_pct == 4.0
    assert s.risk.max_concurrent_positions == 1
    assert s.risk.consecutive_loss_pause == 3
    assert s.risk.kill_switch is False
    assert s.symbols == ["BTCUSD", "ETHUSD"]


def test_env_overrides_and_nested(monkeypatch):
    monkeypatch.setenv("CQ_RISK__KILL_SWITCH", "true")
    monkeypatch.setenv("CQ_SYMBOLS", '["btcusd"]')
    s = Settings()
    assert s.risk.kill_switch is True
    assert s.symbols == ["BTCUSD"]


@pytest.mark.parametrize("mode", ["DEMO", "LIVE"])
def test_real_order_modes_require_credentials(monkeypatch, mode):
    monkeypatch.setenv("CQ_MODE", mode)
    monkeypatch.setenv("CQ_LIVE_TRADING_CONFIRMED", "true")
    with pytest.raises(ValidationError, match="credentials"):
        Settings()


def test_live_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setenv("CQ_MODE", "LIVE")
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match="CONFIRMED"):
        Settings()


def test_live_allowed_only_with_both_guards(monkeypatch):
    monkeypatch.setenv("CQ_MODE", "LIVE")
    monkeypatch.setenv("CQ_LIVE_TRADING_CONFIRMED", "true")
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.mode is ExecutionMode.LIVE and s.allows_real_orders


def test_placeholder_credentials_are_treated_as_missing(monkeypatch):
    monkeypatch.setenv("CQ_EXCHANGE__API_KEY", "your_api_key_here")
    monkeypatch.setenv("CQ_EXCHANGE__API_SECRET", "your_api_secret_here")
    assert Settings().exchange.has_credentials is False
    monkeypatch.setenv("CQ_MODE", "DEMO")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_masked_in_repr(monkeypatch):
    for k, v in CREDS.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.exchange.has_credentials
    assert "s-test" not in repr(s) and "k-test" not in repr(s)
    assert "s-test" not in s.model_dump_json()


def test_blank_urls_become_none(monkeypatch):
    monkeypatch.setenv("CQ_EXCHANGE__BASE_URL", "")
    assert Settings().exchange.base_url is None


@pytest.mark.parametrize(
    "env,value",
    [
        ("CQ_TIMEFRAMES", '["7m"]'),
        ("CQ_SYMBOLS", '["BTCUSD","btcusd"]'),
        ("CQ_SYMBOLS", "[]"),
        ("CQ_LOG_LEVEL", "LOUD"),
        ("CQ_RISK__RISK_PER_TRADE_PCT", "0"),
        ("CQ_RISK__RISK_PER_TRADE_PCT", "50"),
        ("CQ_RISK__DAILY_LOSS_LIMIT_PCT", "5"),  # exceeds weekly 4
        ("CQ_PAPER_INITIAL_EQUITY", "-1"),
    ],
)
def test_invalid_values_rejected(monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValidationError):
        Settings()


def test_risk_per_trade_cannot_exceed_daily_limit(monkeypatch):
    monkeypatch.setenv("CQ_RISK__RISK_PER_TRADE_PCT", "3")
    with pytest.raises(ValidationError, match="daily_loss_limit"):
        Settings()
