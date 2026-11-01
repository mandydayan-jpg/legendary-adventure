"""Central configuration. Everything is read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load squeeze-scanner/.env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


DEFAULT_UNIVERSE = (
    "AAPL,MSFT,NVDA,AMD,META,GOOGL,AMZN,TSLA,AVGO,NFLX,CRM,ADBE,ORCL,PLTR,SNOW,"
    "SHOP,UBER,ABNB,COIN,MU,SMCI,ARM,PANW,CRWD,NET,DDOG,MDB,ZS,TTD,ANET,"
    "LLY,NVO,VRTX,REGN,ISRG,JPM,GS,V,MA,COST,WMT,HD,CAT,DE,GE,BA,LMT,RTX,XOM,CVX"
)


@dataclass(frozen=True)
class Settings:
    # --- Claude ---
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    anthropic_effort: str = os.getenv("ANTHROPIC_EFFORT", "high")
    anthropic_max_tokens: int = _int("ANTHROPIC_MAX_TOKENS", 16000)
    # Server-side refusal fallback ("default" routing). Set to "0" to disable.
    anthropic_fallbacks: bool = os.getenv("ANTHROPIC_FALLBACKS", "1") == "1"

    # --- Optional news providers (yfinance news is always used) ---
    finnhub_api_key: str | None = os.getenv("FINNHUB_API_KEY") or None
    polygon_api_key: str | None = os.getenv("POLYGON_API_KEY") or None

    # --- Strategy rulebook (Knowledge Base) ---
    compression_ratio_max: float = _float("RULE_COMPRESSION_RATIO_MAX", 0.35)  # 5d range < 35% of 30d range
    near_high_max_pct: float = _float("RULE_NEAR_HIGH_MAX_PCT", 0.04)          # within 4% of 30d high
    short_window: int = _int("RULE_SHORT_WINDOW", 5)
    long_window: int = _int("RULE_LONG_WINDOW", 30)
    volume_window: int = _int("RULE_VOLUME_WINDOW", 3)
    news_lookback_days: int = _int("NEWS_LOOKBACK_DAYS", 30)
    news_recent_hours: int = _int("NEWS_RECENT_HOURS", 72)        # top-priority window
    news_critical_days: int = _int("NEWS_CRITICAL_DAYS", 5)        # elevated-weight window
    news_recent_weight: float = _float("NEWS_RECENT_WEIGHT", 0.70)  # 70% weight for the 0-72h bucket

    # --- Trade-level sanity bounds used by the validator ---
    max_stop_distance_pct: float = _float("VALIDATION_MAX_STOP_PCT", 0.15)       # stop no more than 15% below entry
    max_trigger_above_high_pct: float = _float("VALIDATION_MAX_TRIGGER_PCT", 0.05)  # trigger <= 30d high + 5%
    min_trigger_below_high_pct: float = _float("VALIDATION_MIN_TRIGGER_PCT", 0.01)  # trigger >= 30d high - 1%
    min_buy_confidence: int = _int("VALIDATION_MIN_BUY_CONFIDENCE", 60)
    max_data_staleness_days: int = _int("MAX_DATA_STALENESS_DAYS", 5)

    # --- Runtime ---
    universe: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            t.strip().upper() for t in os.getenv("SCAN_UNIVERSE", DEFAULT_UNIVERSE).split(",") if t.strip()
        )
    )
    max_workers: int = _int("MAX_WORKERS", 4)
    http_timeout: float = _float("HTTP_TIMEOUT", 15.0)


settings = Settings()
