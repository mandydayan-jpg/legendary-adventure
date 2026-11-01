"""Market-data collection + deterministic pre-filter for Pre-Breakout Squeeze setups.

All numbers here are computed by code from raw OHLCV bars. Nothing in this module
uses an LLM, so these values are the "ground truth" the AI output is later checked against.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Settings, settings

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


class DataUnavailableError(RuntimeError):
    """Raised when real market data cannot be fetched or is unusable. Never substituted with fake data."""


@dataclass
class TechnicalSnapshot:
    ticker: str
    as_of: str                  # timestamp of the last bar used (ISO, exchange date)
    fetched_at_utc: str
    last_bar_partial: bool      # True if the most recent daily bar is still forming (market open)
    current_price: float
    high_30d: float
    low_30d: float
    range_30d: float
    high_5d: float
    low_5d: float
    range_5d: float
    compression_ratio: float    # range_5d / range_30d
    avg_volume_30d: float
    avg_volume_3d: float
    volume_ratio: float         # avg_volume_3d / avg_volume_30d
    distance_from_high_pct: float  # (high_30d - price) / high_30d
    # rule results
    price_compression: bool
    volume_dry_up: bool
    near_resistance: bool
    conditions_met: int
    # code-derived reference levels (the AI may refine them inside validated bounds)
    reference_entry_trigger: float
    reference_stop_loss: float
    bars: int

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- data fetch
def _is_partial_bar(last_index: pd.Timestamp, now: datetime | None = None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    bar_date = last_index.date() if hasattr(last_index, "date") else last_index
    return bar_date == now.date() and now.weekday() < 5 and time(9, 30) <= now.time() < time(16, 0)


def fetch_history(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """Download real OHLCV bars from Yahoo Finance. Raises DataUnavailableError on any problem."""
    import yfinance as yf  # imported lazily so unit tests don't need network

    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    except Exception as exc:  # network / parsing errors from yfinance
        raise DataUnavailableError(f"{ticker}: price download failed: {exc}") from exc
    if df is None or df.empty:
        raise DataUnavailableError(f"{ticker}: no price data returned")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if df.empty:
        raise DataUnavailableError(f"{ticker}: price data contained only NaNs")
    return df


# --------------------------------------------------------------------------- rule engine
def evaluate_technicals(
    ticker: str,
    df: pd.DataFrame,
    cfg: Settings = settings,
    now: datetime | None = None,
) -> TechnicalSnapshot:
    """Apply the three technical rules of the rulebook to a daily OHLCV frame."""
    long_w, short_w, vol_w = cfg.long_window, cfg.short_window, cfg.volume_window
    if len(df) < long_w + 1:
        raise DataUnavailableError(f"{ticker}: need >= {long_w + 1} daily bars, got {len(df)}")

    now = now or datetime.now(timezone.utc)
    last_idx = df.index[-1]
    last_date = pd.Timestamp(last_idx).tz_localize(None) if pd.Timestamp(last_idx).tzinfo else pd.Timestamp(last_idx)
    staleness = (pd.Timestamp(now.astimezone(NY).date()) - last_date.normalize()).days
    if staleness > cfg.max_data_staleness_days:
        raise DataUnavailableError(f"{ticker}: data is stale (last bar {last_date.date()}, {staleness} days old)")

    partial = _is_partial_bar(pd.Timestamp(last_idx), now)

    last30 = df.iloc[-long_w:]
    last5 = df.iloc[-short_w:]
    high_30, low_30 = float(last30["High"].max()), float(last30["Low"].min())
    high_5, low_5 = float(last5["High"].max()), float(last5["Low"].min())
    range_30, range_5 = high_30 - low_30, high_5 - low_5
    if range_30 <= 0:
        raise DataUnavailableError(f"{ticker}: zero 30-day range (halted / bad data)")

    # Volume: an intraday partial bar would always look like a "dry-up", so use completed bars only.
    vol_df = df.iloc[:-1] if partial else df
    avg_vol_30 = float(vol_df["Volume"].iloc[-long_w:].mean())
    avg_vol_3 = float(vol_df["Volume"].iloc[-vol_w:].mean())
    if avg_vol_30 <= 0:
        raise DataUnavailableError(f"{ticker}: no volume data")

    price = float(df["Close"].iloc[-1])
    compression = range_5 / range_30
    vol_ratio = avg_vol_3 / avg_vol_30
    dist_high = (high_30 - price) / high_30

    c1 = compression < cfg.compression_ratio_max
    c2 = avg_vol_3 < avg_vol_30
    c3 = dist_high <= cfg.near_high_max_pct  # high_30 includes today's high, so dist_high >= 0

    # Reference levels: breakout trigger just above the 30d high, stop just below the 5d consolidation low.
    entry = round(max(high_30, price) * 1.002, 2)
    stop = round(low_5 * 0.995, 2)

    return TechnicalSnapshot(
        ticker=ticker,
        as_of=str(last_date.date()),
        fetched_at_utc=now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        last_bar_partial=partial,
        current_price=round(price, 4),
        high_30d=round(high_30, 4),
        low_30d=round(low_30, 4),
        range_30d=round(range_30, 4),
        high_5d=round(high_5, 4),
        low_5d=round(low_5, 4),
        range_5d=round(range_5, 4),
        compression_ratio=round(compression, 4),
        avg_volume_30d=round(avg_vol_30, 1),
        avg_volume_3d=round(avg_vol_3, 1),
        volume_ratio=round(vol_ratio, 4),
        distance_from_high_pct=round(dist_high, 4),
        price_compression=bool(c1),
        volume_dry_up=bool(c2),
        near_resistance=bool(c3),
        conditions_met=int(c1) + int(c2) + int(c3),
        reference_entry_trigger=entry,
        reference_stop_loss=stop,
        bars=len(df),
    )


def scan_ticker(ticker: str, cfg: Settings = settings) -> TechnicalSnapshot:
    return evaluate_technicals(ticker, fetch_history(ticker), cfg)


def scan_universe(
    tickers: list[str] | tuple[str, ...] | None = None,
    min_conditions: int = 3,
    cfg: Settings = settings,
) -> tuple[list[TechnicalSnapshot], list[TechnicalSnapshot], dict[str, str]]:
    """Scan many tickers in parallel.

    Returns (passed, all_snapshots, errors). `passed` = snapshots meeting >= min_conditions rules.
    Tickers whose data could not be fetched are reported in `errors` - never guessed.
    """
    tickers = [t.strip().upper() for t in (tickers or cfg.universe) if t and t.strip()]
    snapshots: list[TechnicalSnapshot] = []
    errors: dict[str, str] = {}

    def work(t: str):
        try:
            return t, scan_ticker(t, cfg), None
        except DataUnavailableError as exc:
            return t, None, str(exc)
        except Exception as exc:  # defensive: one bad ticker must not kill the scan
            log.exception("unexpected error scanning %s", t)
            return t, None, f"{t}: unexpected error: {exc}"

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as pool:
        for t, snap, err in pool.map(work, tickers):
            if snap:
                snapshots.append(snap)
            else:
                errors[t] = err or "unknown error"

    snapshots.sort(key=lambda s: (-s.conditions_met, s.compression_ratio))
    passed = [s for s in snapshots if s.conditions_met >= min_conditions]
    return passed, snapshots, errors


def history_for_chart(ticker: str, days: int = 90) -> list[dict]:
    df = fetch_history(ticker, period="1y").iloc[-days:]
    return [
        {
            "date": str(pd.Timestamp(idx).date()),
            "open": float(r.Open),
            "high": float(r.High),
            "low": float(r.Low),
            "close": float(r.Close),
            "volume": float(r.Volume),
        }
        for idx, r in df.iterrows()
    ]
