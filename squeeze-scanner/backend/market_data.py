"""Daily OHLCV providers with an ordered fallback chain.

PRICE_PROVIDERS (env) sets the order, e.g. "twelvedata,alphavantage,yfinance".
Providers whose API key is missing are skipped. The first provider that returns
usable real data wins; if all fail, DataUnavailableError lists every failure.
No provider ever fabricates or interpolates bars.
"""
from __future__ import annotations

import logging

import pandas as pd
import requests

from .config import Settings, settings

log = logging.getLogger(__name__)
COLS = ["Open", "High", "Low", "Close", "Volume"]


class DataUnavailableError(RuntimeError):
    """Raised when real market data cannot be fetched or is unusable. Never substituted with fake data."""


def _finalize(df: pd.DataFrame, source: str) -> pd.DataFrame:
    df = df[COLS].apply(pd.to_numeric, errors="coerce").dropna().sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.attrs["source"] = source
    return df


# --------------------------------------------------------------------------- providers
def _yfinance(ticker: str, bars: int, cfg: Settings) -> pd.DataFrame:
    import yfinance as yf  # lazy import: unit tests need no network

    period = "1y" if bars > 120 else "6mo"
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise DataUnavailableError("no price data returned")
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return _finalize(df, "yfinance")


def _alphavantage(ticker: str, bars: int, cfg: Settings) -> pd.DataFrame:
    r = requests.get(
        "https://www.alphavantage.co/query",
        params={"function": "TIME_SERIES_DAILY", "symbol": ticker,
                "outputsize": "compact" if bars <= 100 else "full", "apikey": cfg.alphavantage_api_key},
        timeout=cfg.http_timeout,
    )
    r.raise_for_status()
    js = r.json()
    series = js.get("Time Series (Daily)")
    if not series:
        # Alpha Vantage signals errors / rate limits with HTTP 200 + one of these keys
        msg = js.get("Error Message") or js.get("Note") or js.get("Information") or "empty response"
        raise DataUnavailableError(f"alphavantage: {str(msg)[:200]}")
    df = pd.DataFrame.from_dict(series, orient="index")
    df.index = pd.to_datetime(df.index)
    df = df.rename(columns={"1. open": "Open", "2. high": "High", "3. low": "Low",
                            "4. close": "Close", "5. volume": "Volume"})
    return _finalize(df, "alphavantage")


def _twelvedata(ticker: str, bars: int, cfg: Settings) -> pd.DataFrame:
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": ticker, "interval": "1day", "outputsize": max(bars, 60), "apikey": cfg.twelvedata_api_key},
        timeout=cfg.http_timeout,
    )
    r.raise_for_status()
    js = r.json()
    if js.get("status") == "error" or not js.get("values"):
        raise DataUnavailableError(f"twelvedata: {str(js.get('message') or 'empty response')[:200]}")
    df = pd.DataFrame(js["values"])
    df.index = pd.to_datetime(df["datetime"])
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return _finalize(df, "twelvedata")


PROVIDERS = {
    "yfinance": (_yfinance, lambda cfg: True),
    "alphavantage": (_alphavantage, lambda cfg: bool(cfg.alphavantage_api_key)),
    "twelvedata": (_twelvedata, lambda cfg: bool(cfg.twelvedata_api_key)),
}


def active_providers(cfg: Settings = settings) -> list[str]:
    return [p for p in cfg.price_providers if p in PROVIDERS and PROVIDERS[p][1](cfg)]


def fetch_daily_bars(ticker: str, bars: int = 130, cfg: Settings = settings) -> pd.DataFrame:
    """Return real daily bars from the first provider in the chain that succeeds."""
    failures: list[str] = []
    for name in active_providers(cfg):
        fn = PROVIDERS[name][0]
        try:
            df = fn(ticker, bars, cfg)
            if df.empty:
                raise DataUnavailableError("only NaN rows")
            return df
        except Exception as exc:
            log.info("price provider %s failed for %s: %s", name, ticker, exc)
            failures.append(f"{name}: {str(exc)[:200]}")
    raise DataUnavailableError(f"{ticker}: all price providers failed -> " + " | ".join(failures or ["none configured"]))
