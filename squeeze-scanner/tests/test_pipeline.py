"""Offline tests: rule engine, news weighting, validator and the analyzer with a fake Claude client."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.ai_analyzer import ClaudeAnalyzer
from backend.config import Settings
from backend.models import AIAnalysis
from backend.news_engine import BUCKET_BASELINE, BUCKET_CRITICAL, BUCKET_RECENT, NewsArticle, NewsBundle, assign_weights, bucket_for
from backend.scanner import DataUnavailableError, evaluate_technicals
from backend.validator import validate_analysis

NOW = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)  # Friday, after the US close
CFG = Settings()


def squeeze_frame() -> pd.DataFrame:
    """60 bars: wide range early, then a tight 5-day coil right under the 30d high on falling volume."""
    idx = pd.bdate_range(end="2026-10-30", periods=60)
    close = np.concatenate([np.linspace(80, 100, 45), np.linspace(100, 92, 10), [98.6, 98.9, 99.1, 98.8, 99.2]])
    high = close + 1.0
    low = close - 1.0
    high[-5:], low[-5:] = close[-5:] + 0.4, close[-5:] - 0.4
    vol = np.full(60, 1_000_000.0)
    vol[-3:] = 500_000
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)


def make_news() -> NewsBundle:
    arts = [
        NewsArticle("N1", "yfinance", "Reuters", "Acme wins $2 billion defense contract",
                    "Acme said on Thursday it won a $2 billion multi-year contract.", "https://x/1",
                    (NOW - timedelta(hours=20)).isoformat(), (NOW - timedelta(hours=20)).date().isoformat(),
                    20.0, BUCKET_RECENT, 0),
        NewsArticle("N2", "yfinance", "AP", "Acme raises full-year guidance", "", "https://x/2",
                    (NOW - timedelta(days=12)).isoformat(), (NOW - timedelta(days=12)).date().isoformat(),
                    288.0, BUCKET_BASELINE, 0),
    ]
    shares = assign_weights(arts, CFG)
    return NewsBundle("ACME", NOW.isoformat(), 30, arts, {}, {BUCKET_RECENT: 1, BUCKET_CRITICAL: 0, BUCKET_BASELINE: 1, "total": 2}, shares)


def good_ai(tech, news) -> dict:
    return {
        "ticker": "ACME", "data_verified": True, "confidence_score": 78, "recommendation": "BUY_WATCHLIST",
        "entry_trigger_price": tech.reference_entry_trigger, "stop_loss_price": tech.reference_stop_loss,
        "conditions_check": {
            "price_compression": {"met": True, "evidence": f"compression_ratio {tech.compression_ratio} < 0.35"},
            "volume_dry_up": {"met": True, "evidence": f"volume_ratio {tech.volume_ratio} < 1"},
            "near_resistance": {"met": True, "evidence": f"distance_from_high_pct {tech.distance_from_high_pct}"},
            "catalyst_news": {"met": True, "evidence": f'"Acme wins $2 billion defense contract" ({news.articles[0].published_date}, N1)',
                              "citations": [{"article_id": "N1", "quote": "Acme wins $2 billion defense contract",
                                             "date": news.articles[0].published_date}]},
        },
        "news_analysis": {"recent_catalysts_score": 85, "historical_baseline_score": 60,
                          "time_weighted_sentiment": "BULLISH_RECENT",
                          "key_recent_event": "N1", "key_historical_event": "N2"},
        "summary_reasoning": "All rules met.",
    }


# ------------------------------------------------------------------ scanner
def test_rules_detect_squeeze():
    t = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW)
    assert t.price_compression and t.volume_dry_up and t.near_resistance
    assert t.conditions_met == 3
    assert t.reference_stop_loss < t.current_price < t.reference_entry_trigger
    assert not t.last_bar_partial


def test_wide_range_fails_compression():
    df = squeeze_frame()
    df.iloc[-3, df.columns.get_loc("Low")] = 70  # blow out the 5-day range
    assert not evaluate_technicals("ACME", df, CFG, now=NOW).price_compression


def test_stale_data_rejected():
    with pytest.raises(DataUnavailableError):
        evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW + timedelta(days=20))


def test_partial_bar_excluded_from_volume():
    df = squeeze_frame()
    df.iloc[-1, df.columns.get_loc("Volume")] = 1  # tiny intraday volume
    intraday = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)  # 11:00 New York
    t = evaluate_technicals("ACME", df, CFG, now=intraday)
    assert t.last_bar_partial
    assert t.avg_volume_3d > 1000


# ------------------------------------------------------------------ news weighting
def test_buckets_and_weights():
    assert bucket_for(10, CFG) == BUCKET_RECENT
    assert bucket_for(100, CFG) == BUCKET_CRITICAL
    assert bucket_for(200, CFG) == BUCKET_BASELINE
    news = make_news()
    assert news.articles[0].weight == pytest.approx(0.70, abs=1e-4)
    assert sum(a.weight for a in news.articles) <= 1.0


# ------------------------------------------------------------------ validator
def test_valid_answer_passes():
    tech, news = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW), make_news()
    ai, rep = validate_analysis(AIAnalysis.model_validate(good_ai(tech, news)), tech, news, CFG)
    assert rep.passed, rep.errors
    assert ai.recommendation == "BUY_WATCHLIST"


def test_fabricated_quote_rejected():
    tech, news = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW), make_news()
    d = good_ai(tech, news)
    d["conditions_check"]["catalyst_news"]["citations"][0]["quote"] = "Acme signs $5 billion deal with NASA"
    ai, rep = validate_analysis(AIAnalysis.model_validate(d), tech, news, CFG)
    assert not rep.passed and ai.recommendation == "REJECT"


def test_wrong_date_rejected():
    tech, news = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW), make_news()
    d = good_ai(tech, news)
    d["conditions_check"]["catalyst_news"]["citations"][0]["date"] = "2026-01-01"
    _, rep = validate_analysis(AIAnalysis.model_validate(d), tech, news, CFG)
    assert any("date" in e for e in rep.errors)


def test_stop_above_entry_rejected():
    tech, news = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW), make_news()
    d = good_ai(tech, news)
    d["stop_loss_price"] = d["entry_trigger_price"] + 1
    _, rep = validate_analysis(AIAnalysis.model_validate(d), tech, news, CFG)
    assert any("not below entry" in e for e in rep.errors)


def test_absurd_trigger_rejected():
    tech, news = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW), make_news()
    d = good_ai(tech, news)
    d["entry_trigger_price"] = tech.high_30d * 1.5
    _, rep = validate_analysis(AIAnalysis.model_validate(d), tech, news, CFG)
    assert not rep.passed


def test_technical_flag_overridden_and_buy_downgraded():
    df = squeeze_frame()
    df.iloc[-3, df.columns.get_loc("Low")] = 70
    tech, news = evaluate_technicals("ACME", df, CFG, now=NOW), make_news()
    d = good_ai(tech, news)
    d["stop_loss_price"] = round(tech.current_price * 0.95, 2)
    ai, rep = validate_analysis(AIAnalysis.model_validate(d), tech, news, CFG)
    assert "conditions_check.price_compression.met" in rep.overrides
    assert ai.conditions_check.price_compression.met is False
    assert ai.recommendation == "WAIT"


def test_recent_score_without_recent_news_rejected():
    tech, news = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW), make_news()
    news.articles = news.articles[1:]
    news.counts = {BUCKET_RECENT: 0, BUCKET_CRITICAL: 0, BUCKET_BASELINE: 1, "total": 1}
    d = good_ai(tech, news)
    d["conditions_check"]["catalyst_news"]["citations"] = []
    _, rep = validate_analysis(AIAnalysis.model_validate(d), tech, news, CFG)
    assert not rep.passed


# ------------------------------------------------------------------ analyzer with fake client
class FakeClient:
    def __init__(self, payload: dict):
        self.payload, self.calls = payload, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", model=kwargs["model"],
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_analyzer_end_to_end_with_fake_claude():
    tech, news = evaluate_technicals("ACME", squeeze_frame(), CFG, now=NOW), make_news()
    fake = FakeClient(good_ai(tech, news))
    res = ClaudeAnalyzer(CFG, client=fake).analyze(tech, news)
    assert res.status == "OK", (res.error, res.validation)
    assert res.time_weighted_news_score == pytest.approx(0.7 * 85 + 0.3 * 60)
    call = fake.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"
    assert "<news_data>" in call["messages"][0]["content"]


# ------------------------------------------------------------------ market-data providers (mocked HTTP)
from dataclasses import replace  # noqa: E402

from backend import market_data, news_engine  # noqa: E402


class FakeResp:
    def __init__(self, js):
        self._js = js

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


def test_alphavantage_parsing(monkeypatch):
    js = {"Time Series (Daily)": {
        "2026-10-30": {"1. open": "10", "2. high": "11", "3. low": "9", "4. close": "10.5", "5. volume": "1000"},
        "2026-10-29": {"1. open": "9", "2. high": "10", "3. low": "8", "4. close": "9.5", "5. volume": "900"}}}
    monkeypatch.setattr(market_data.requests, "get", lambda *a, **k: FakeResp(js))
    df = market_data._alphavantage("ACME", 100, replace(CFG, alphavantage_api_key="k"))
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-10-29", "2026-10-30"]  # sorted ascending
    assert df.attrs["source"] == "alphavantage" and df["Close"].iloc[-1] == 10.5


def test_alphavantage_rate_limit_is_an_error(monkeypatch):
    monkeypatch.setattr(market_data.requests, "get", lambda *a, **k: FakeResp({"Information": "rate limit"}))
    with pytest.raises(market_data.DataUnavailableError, match="rate limit"):
        market_data._alphavantage("ACME", 100, replace(CFG, alphavantage_api_key="k"))


def test_twelvedata_parsing(monkeypatch):
    js = {"status": "ok", "values": [
        {"datetime": "2026-10-30", "open": "10", "high": "11", "low": "9", "close": "10.5", "volume": "1000"},
        {"datetime": "2026-10-29", "open": "9", "high": "10", "low": "8", "close": "9.5", "volume": "900"}]}
    monkeypatch.setattr(market_data.requests, "get", lambda *a, **k: FakeResp(js))
    df = market_data._twelvedata("ACME", 100, replace(CFG, twelvedata_api_key="k"))
    assert df.index[-1].strftime("%Y-%m-%d") == "2026-10-30" and df.attrs["source"] == "twelvedata"


def test_provider_chain_falls_back_and_reports_all_failures(monkeypatch):
    cfg = replace(CFG, twelvedata_api_key="k", alphavantage_api_key=None, price_providers=("twelvedata", "alphavantage", "yfinance"))
    assert market_data.active_providers(cfg) == ["twelvedata", "yfinance"]  # alphavantage skipped: no key

    def boom(*a, **k):
        raise market_data.DataUnavailableError("down")
    monkeypatch.setitem(market_data.PROVIDERS, "twelvedata", (boom, lambda c: True))
    monkeypatch.setitem(market_data.PROVIDERS, "yfinance", (lambda t, b, c: market_data._finalize(squeeze_frame(), "yfinance"), lambda c: True))
    assert market_data.fetch_daily_bars("ACME", 60, cfg).attrs["source"] == "yfinance"

    monkeypatch.setitem(market_data.PROVIDERS, "yfinance", (boom, lambda c: True))
    with pytest.raises(market_data.DataUnavailableError, match="twelvedata: down.*yfinance: down"):
        market_data.fetch_daily_bars("ACME", 60, cfg)


def test_alphavantage_news_parsing(monkeypatch):
    js = {"feed": [{"title": "Acme wins contract", "summary": "s", "url": "u", "source": "Benzinga",
                    "time_published": "20261030T153000"}]}
    monkeypatch.setattr(news_engine.requests, "get", lambda *a, **k: FakeResp(js))
    out = news_engine._from_alphavantage("ACME", NOW - timedelta(days=30), replace(CFG, alphavantage_api_key="k"))
    assert out[0]["ts"] == datetime(2026, 10, 30, 19, 30, tzinfo=timezone.utc)  # 15:30 New York (EDT) -> 19:30 UTC
