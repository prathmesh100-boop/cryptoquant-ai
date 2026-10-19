"""Validated application settings.

Settings load from environment variables (prefix ``CQ_``) and an optional ``.env``
file. Nested groups use a double underscore, e.g. ``CQ_RISK__KILL_SWITCH=true``.

Safety rules enforced here (not just documented):
* The default mode is ``PAPER``.
* ``DEMO`` and ``LIVE`` require exchange credentials.
* ``LIVE`` additionally requires ``CQ_LIVE_TRADING_CONFIRMED=true``.
* Placeholder credentials from ``.env.example`` are treated as missing.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_TIMEFRAMES: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

_PLACEHOLDER_VALUES = {"", "your_api_key_here", "your_api_secret_here", "changeme"}


class ExecutionMode(StrEnum):
    """Explicit execution modes. Never switched implicitly."""

    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"


class RiskSettings(BaseModel):
    """Initial research risk parameters. These are testing values, not safety guarantees."""

    risk_per_trade_pct: float = Field(0.5, gt=0, le=5.0, description="% of allocated equity risked per trade")
    daily_loss_limit_pct: float = Field(2.0, gt=0, le=20.0)
    weekly_loss_limit_pct: float = Field(4.0, gt=0, le=40.0)
    max_concurrent_positions: int = Field(1, ge=1, le=10)
    consecutive_loss_pause: int = Field(3, ge=1, le=20)
    max_leverage: float = Field(3.0, ge=1.0, le=20.0)
    max_position_notional: float | None = Field(None, gt=0)
    kill_switch: bool = False

    @model_validator(mode="after")
    def _limits_are_coherent(self) -> RiskSettings:
        if self.weekly_loss_limit_pct < self.daily_loss_limit_pct:
            raise ValueError("weekly_loss_limit_pct must be >= daily_loss_limit_pct")
        if self.risk_per_trade_pct > self.daily_loss_limit_pct:
            raise ValueError("risk_per_trade_pct must not exceed daily_loss_limit_pct")
        return self


class ExchangeSettings(BaseModel):
    """Exchange connection settings. Endpoints are verified against official docs in Phase 7."""

    name: str = "delta_india"
    base_url: str | None = None
    ws_url: str | None = None
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    request_timeout_s: float = Field(10.0, gt=0, le=60)

    @field_validator("base_url", "ws_url", mode="before")
    @classmethod
    def _blank_url_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _placeholder_is_none(cls, v: object) -> object:
        raw = v.get_secret_value() if isinstance(v, SecretStr) else v
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in _PLACEHOLDER_VALUES):
            return None
        return v

    @property
    def has_credentials(self) -> bool:
        return self.api_key is not None and self.api_secret is not None


class DataSettings(BaseModel):
    """Market-data validation parameters."""

    max_staleness_candles: int = Field(2, ge=1, le=20, description="Data older than N candle periods is stale")
    min_history_candles: int = Field(250, ge=50)


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="CQ_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: ExecutionMode = ExecutionMode.PAPER
    live_trading_confirmed: bool = False
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSD", "ETHUSD"])
    timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h", "4h"])
    paper_initial_equity: float = Field(10_000.0, gt=0)
    database_url: str = "sqlite:///data/cryptoquant.db"
    log_level: str = "INFO"
    log_json: bool = True

    risk: RiskSettings = Field(default_factory=RiskSettings)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    data: DataSettings = Field(default_factory=DataSettings)

    @field_validator("symbols")
    @classmethod
    def _symbols_valid(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip().upper() for s in v if s.strip()]
        if not cleaned:
            raise ValueError("at least one symbol is required")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate symbols are not allowed")
        return cleaned

    @field_validator("timeframes")
    @classmethod
    def _timeframes_valid(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in SUPPORTED_TIMEFRAMES]
        if unknown:
            raise ValueError(f"unsupported timeframes: {unknown}; supported: {sorted(SUPPORTED_TIMEFRAMES)}")
        if not v:
            raise ValueError("at least one timeframe is required")
        return v

    @field_validator("log_level")
    @classmethod
    def _log_level_valid(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {v}")
        return level

    @model_validator(mode="after")
    def _mode_safety(self) -> Settings:
        if self.mode in (ExecutionMode.DEMO, ExecutionMode.LIVE) and not self.exchange.has_credentials:
            raise ValueError(f"mode {self.mode} requires exchange API credentials")
        if self.mode is ExecutionMode.LIVE and not self.live_trading_confirmed:
            raise ValueError("mode LIVE requires CQ_LIVE_TRADING_CONFIRMED=true")
        return self

    @property
    def allows_real_orders(self) -> bool:
        """True only for LIVE, or DEMO against the exchange test environment."""
        return self.mode in (ExecutionMode.DEMO, ExecutionMode.LIVE)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process. Tests should construct ``Settings`` directly."""
    return Settings()
