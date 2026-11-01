"""Strict Pydantic schemas.

`AIAnalysis` is the hard JSON contract Claude must return. It is passed to the API as a
structured-output schema (so the model is constrained to it) AND re-validated by Pydantic
on our side. `citations` is an addition to the requested format: it carries the machine-
checkable proof (article id + verbatim quote + date) behind the catalyst evidence text.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(_Strict):
    met: bool
    evidence: str = Field(description="Facts only, quoting the exact numbers from the TECHNICAL_DATA block.")


class Citation(_Strict):
    article_id: str = Field(description="The id of the article in NEWS_DATA, e.g. 'N3'.")
    quote: str = Field(description="A verbatim, character-for-character excerpt from that article's title or summary.")
    date: str = Field(description="That article's published_date exactly as given (YYYY-MM-DD).")


class CatalystCondition(_Strict):
    met: bool
    evidence: str = Field(description='Exact quote and date, e.g. "\\"<quote>\\" (2026-10-30, N2)". "NO_QUALIFYING_NEWS" if none.')
    citations: list[Citation] = Field(description="Every news claim must be backed by an entry here. Empty if met is false.")


class ConditionsCheck(_Strict):
    price_compression: Condition
    volume_dry_up: Condition
    near_resistance: Condition
    catalyst_news: CatalystCondition


class NewsAnalysis(_Strict):
    recent_catalysts_score: int = Field(ge=0, le=100, description="0-100, based ONLY on RECENT_0_72H articles.")
    historical_baseline_score: int = Field(ge=0, le=100, description="0-100, based on CRITICAL_3_5D and BASELINE_5_30D articles.")
    time_weighted_sentiment: Literal["BULLISH_RECENT", "NEUTRAL", "BEARISH"]
    key_recent_event: str = Field(description='Quote + date of the most important RECENT_0_72H article, or "NONE".')
    key_historical_event: str = Field(description='Quote + date of the most important older article, or "NONE".')


class AIAnalysis(_Strict):
    ticker: str
    data_verified: bool = Field(description="true only if every statement is backed by the supplied data.")
    confidence_score: int = Field(ge=0, le=100)
    recommendation: Literal["BUY_WATCHLIST", "WAIT", "REJECT"]
    entry_trigger_price: float
    stop_loss_price: float
    conditions_check: ConditionsCheck
    news_analysis: NewsAnalysis
    summary_reasoning: str = Field(description="Fact-based explanation using only supplied data.")


# ----------------------------------------------------------------------------- API models
class ValidationReport(BaseModel):
    passed: bool
    errors: list[str]      # hard failures -> recommendation forced to REJECT
    warnings: list[str]    # soft issues, shown to the user
    overrides: dict[str, str]  # fields the server corrected from ground truth


class AnalysisResult(BaseModel):
    ticker: str
    status: Literal["OK", "VALIDATION_FAILED", "AI_ERROR", "DATA_ERROR"]
    analysis: AIAnalysis | None = None
    validation: ValidationReport | None = None
    technicals: dict | None = None
    news: dict | None = None
    time_weighted_news_score: float | None = None
    model: str | None = None
    error: str | None = None
    analyzed_at_utc: str


class ScanRequest(BaseModel):
    tickers: list[str] | None = Field(default=None, description="Defaults to SCAN_UNIVERSE.")
    min_conditions: int = Field(default=3, ge=0, le=3, description="Technical rules a ticker must pass before AI analysis.")
    analyze: bool = True
    max_ai_calls: int = Field(default=10, ge=0, le=100)
