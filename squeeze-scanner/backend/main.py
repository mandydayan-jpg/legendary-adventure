"""FastAPI server: orchestrates scan -> news -> Claude -> validation and serves results to the frontend.

Run from the `squeeze-scanner/` directory:
    uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .ai_analyzer import ClaudeAnalyzer, analyze_ticker
from .config import settings
from .models import AnalysisResult, ScanRequest
from .news_engine import collect_news
from .scanner import DataUnavailableError, history_for_chart, scan_ticker, scan_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("squeeze")

app = FastAPI(title="Pre-Breakout Squeeze Scanner", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_analyzer: ClaudeAnalyzer | None = None
_analyzer_lock = threading.Lock()
_latest_scan: dict | None = None
_scan_lock = threading.Lock()  # one full scan at a time (protects API quotas)


def get_analyzer() -> ClaudeAnalyzer:
    global _analyzer
    with _analyzer_lock:
        if _analyzer is None:
            try:
                _analyzer = ClaudeAnalyzer(settings)
            except Exception as exc:
                raise HTTPException(503, f"Claude client could not be initialised: {exc}") from exc
        return _analyzer


def _ticker(t: str) -> str:
    t = t.strip().upper()
    if not t or len(t) > 12 or not all(ch.isalnum() or ch in ".-^=" for ch in t):
        raise HTTPException(400, f"Invalid ticker '{t}'")
    return t


@app.get("/health")
def health():
    import os

    return {
        "status": "ok",
        "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": settings.anthropic_model,
        "effort": settings.anthropic_effort,
        "anthropic_key_configured": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
        "news_providers": ["yfinance"]
        + (["finnhub"] if settings.finnhub_api_key else [])
        + (["polygon"] if settings.polygon_api_key else []),
        "universe_size": len(settings.universe),
    }


@app.get("/rules")
def rules():
    s = settings
    return {
        "price_compression": f"{s.short_window}d range < {s.compression_ratio_max:.0%} of {s.long_window}d range",
        "volume_dry_up": f"avg volume last {s.volume_window}d < avg volume {s.long_window}d",
        "near_resistance": f"price within {s.near_high_max_pct:.0%} of {s.long_window}d high",
        "catalyst_news": f"news in last {s.news_lookback_days}d; 0-{s.news_recent_hours}h bucket = "
                         f"{s.news_recent_weight:.0%} weight; 3-{s.news_critical_days}d elevated",
    }


@app.get("/universe")
def universe():
    return {"tickers": list(settings.universe)}


@app.post("/scan")
def run_scan(req: ScanRequest):
    """Technical pre-filter over the universe, then Claude analysis for tickers that pass."""
    global _latest_scan
    if not _scan_lock.acquire(blocking=False):
        raise HTTPException(409, "A scan is already running.")
    try:
        t0 = time.time()
        tickers = [_ticker(t) for t in req.tickers] if req.tickers else None
        passed, all_snaps, errors = scan_universe(tickers, req.min_conditions, settings)

        results: list[AnalysisResult] = []
        skipped_ai: list[str] = []
        if req.analyze and passed:
            analyzer = get_analyzer()
            to_analyze = passed[: req.max_ai_calls]
            skipped_ai = [s.ticker for s in passed[req.max_ai_calls:]]
            with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
                results = list(pool.map(lambda s: analyze_ticker(s.ticker, analyzer, settings, tech=s), to_analyze))

        order = {"BUY_WATCHLIST": 0, "WAIT": 1, "REJECT": 2}
        results.sort(key=lambda r: (
            r.status != "OK",
            order.get(r.analysis.recommendation, 3) if r.analysis else 3,
            -(r.analysis.confidence_score if r.analysis else 0),
        ))

        payload = {
            "scanned_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_sec": round(time.time() - t0, 1),
            "requested": len(tickers or settings.universe),
            "scanned_ok": len(all_snaps),
            "passed_prefilter": [s.ticker for s in passed],
            "skipped_ai_due_to_limit": skipped_ai,
            "results": [r.model_dump() for r in results],
            "technical_snapshots": [s.to_dict() for s in all_snaps],
            "data_errors": errors,
        }
        _latest_scan = payload
        return payload
    finally:
        _scan_lock.release()


@app.get("/scan/latest")
def latest_scan():
    if _latest_scan is None:
        raise HTTPException(404, "No scan has been run yet.")
    return _latest_scan


@app.get("/analyze/{ticker}", response_model=AnalysisResult)
def analyze_one(ticker: str):
    """Run the full pipeline for a single ticker regardless of the pre-filter result."""
    return analyze_ticker(_ticker(ticker), get_analyzer(), settings)


@app.get("/technicals/{ticker}")
def technicals(ticker: str):
    try:
        return scan_ticker(_ticker(ticker), settings).to_dict()
    except DataUnavailableError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/news/{ticker}")
def news(ticker: str):
    return collect_news(_ticker(ticker), settings).to_dict()


@app.get("/history/{ticker}")
def history(ticker: str, days: int = Query(90, ge=10, le=250)):
    try:
        return {"ticker": ticker.upper(), "bars": history_for_chart(_ticker(ticker), days)}
    except DataUnavailableError as exc:
        raise HTTPException(502, str(exc)) from exc
