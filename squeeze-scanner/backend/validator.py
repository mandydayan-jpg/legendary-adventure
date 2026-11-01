"""Server-side validation of Claude's output against ground-truth data.

Nothing the model returns reaches the user until it passes these checks:
  * prices are sane (stop < entry, stop < current price, trigger near the 30d high, bounded risk)
  * technical `met` flags match what the code computed (ground truth wins)
  * every news citation exists, is quoted verbatim, and carries the article's exact date
  * news scores are consistent with what news actually exists in each time bucket
  * BUY_WATCHLIST is only allowed when the rulebook is fully satisfied
Hard errors force the result to REJECT; soft problems become warnings / overrides.
"""
from __future__ import annotations

import math
import re

from .config import Settings, settings
from .models import AIAnalysis, ValidationReport
from .news_engine import BUCKET_RECENT, NewsBundle
from .scanner import TechnicalSnapshot

_RULE_CONSTANTS = {3, 4, 5, 30, 35, 48, 70, 72, 100}


def _norm(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def _numbers_in(text: str) -> list[float]:
    out = []
    for m in re.findall(r"-?\d[\d,]*\.?\d*", text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _number_is_grounded(n: float, truth: list[float]) -> bool:
    if n in _RULE_CONSTANTS:
        return True
    for t in truth:
        for candidate in (t, t * 100):  # allow ratios expressed as percentages
            if candidate == 0:
                if abs(n) < 1e-9:
                    return True
                continue
            if abs(n - candidate) / abs(candidate) <= 0.01 or abs(n - candidate) <= 0.011:
                return True
    return False


def validate_analysis(
    ai: AIAnalysis,
    tech: TechnicalSnapshot,
    news: NewsBundle,
    cfg: Settings = settings,
) -> tuple[AIAnalysis, ValidationReport]:
    errors: list[str] = []
    warnings: list[str] = []
    overrides: dict[str, str] = {}
    ai = ai.model_copy(deep=True)
    cc = ai.conditions_check

    # ---- identity
    if ai.ticker.strip().upper() != tech.ticker.upper():
        errors.append(f"Ticker mismatch: AI returned '{ai.ticker}', expected '{tech.ticker}'.")

    if not ai.data_verified:
        errors.append("Model reported data_verified=false (it could not ground its answer in the supplied data).")

    # ---- prices
    entry, stop, price, high30 = ai.entry_trigger_price, ai.stop_loss_price, tech.current_price, tech.high_30d
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop)):
        errors.append(f"Entry/stop must be positive finite numbers (entry={entry}, stop={stop}).")
    else:
        if stop >= entry:
            errors.append(f"Stop-loss {stop} is not below entry trigger {entry}.")
        if stop >= price:
            errors.append(f"Stop-loss {stop} is at/above the current price {price} (would trigger immediately).")
        lo = high30 * (1 - cfg.min_trigger_below_high_pct)
        hi = max(high30, price) * (1 + cfg.max_trigger_above_high_pct)
        if not (lo <= entry <= hi):
            errors.append(
                f"Entry trigger {entry} is outside the logical breakout zone [{lo:.2f}, {hi:.2f}] "
                f"around the 30-day high {high30}."
            )
        if entry < price * 0.995:
            errors.append(f"Entry trigger {entry} is below the current price {price}; a breakout trigger must be above price.")
        risk = (entry - stop) / entry if entry else 1
        if risk > cfg.max_stop_distance_pct:
            errors.append(f"Stop is {risk:.1%} below entry (max allowed {cfg.max_stop_distance_pct:.0%}).")

    # ---- technical conditions: ground truth from code wins
    truth = {
        "price_compression": tech.price_compression,
        "volume_dry_up": tech.volume_dry_up,
        "near_resistance": tech.near_resistance,
    }
    truth_numbers = [v for v in tech.to_dict().values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for name, expected in truth.items():
        cond = getattr(cc, name)
        if cond.met != expected:
            overrides[f"conditions_check.{name}.met"] = f"{cond.met} -> {expected} (computed from market data)"
            cond.met = expected
        ungrounded = [n for n in _numbers_in(cond.evidence) if not _number_is_grounded(n, truth_numbers)]
        if ungrounded:
            warnings.append(f"{name}: evidence contains numbers not found in the technical data: {ungrounded[:5]}")

    # ---- news citations
    articles = news.by_id()
    valid_citations = 0
    for c in cc.catalyst_news.citations:
        art = articles.get(c.article_id.strip())
        if art is None:
            errors.append(f"Citation references unknown article id '{c.article_id}'.")
            continue
        if len(c.quote.strip()) < 12:
            errors.append(f"Citation {c.article_id}: quote too short to verify ('{c.quote}').")
            continue
        if _norm(c.quote) not in _norm(f"{art.title} {art.summary}"):
            errors.append(f"Citation {c.article_id}: quote is not a verbatim excerpt of the article: '{c.quote[:120]}'.")
            continue
        if c.date.strip() != art.published_date:
            errors.append(f"Citation {c.article_id}: date '{c.date}' does not match article date {art.published_date}.")
            continue
        valid_citations += 1

    if cc.catalyst_news.met and valid_citations == 0:
        errors.append("catalyst_news.met is true but no valid, verifiable citation was provided.")
    if news.counts.get("total", 0) == 0 and cc.catalyst_news.met:
        errors.append("catalyst_news.met is true but no news articles exist in the 30-day window.")

    # ---- news scores vs. what actually exists
    na = ai.news_analysis
    recent_count = news.counts.get(BUCKET_RECENT, 0)
    older_count = news.counts.get("total", 0) - recent_count
    if recent_count == 0:
        if na.recent_catalysts_score > 5:
            errors.append(f"recent_catalysts_score={na.recent_catalysts_score} but there are no articles in the last 72h.")
        if na.time_weighted_sentiment == "BULLISH_RECENT":
            errors.append("Sentiment BULLISH_RECENT claimed without any article from the last 72h.")
    if older_count == 0 and na.historical_baseline_score > 5:
        errors.append(f"historical_baseline_score={na.historical_baseline_score} but there are no older articles.")

    # ---- recommendation consistency with the rulebook
    all_technical = all(truth.values())
    if ai.recommendation == "BUY_WATCHLIST":
        reasons = []
        if not all_technical:
            reasons.append("not all technical conditions are met")
        if not cc.catalyst_news.met:
            reasons.append("no verified catalyst")
        if ai.confidence_score < cfg.min_buy_confidence:
            reasons.append(f"confidence {ai.confidence_score} < {cfg.min_buy_confidence}")
        if reasons:
            overrides["recommendation"] = f"BUY_WATCHLIST -> WAIT ({'; '.join(reasons)})"
            ai.recommendation = "WAIT"

    if errors:
        overrides["recommendation"] = f"{ai.recommendation} -> REJECT (failed validation)"
        ai.recommendation = "REJECT"
        ai.data_verified = False

    return ai, ValidationReport(passed=not errors, errors=errors, warnings=warnings, overrides=overrides)


def time_weighted_news_score(ai: AIAnalysis, cfg: Settings = settings) -> float:
    """Deterministic 70/30 blend of the AI's bucket scores."""
    w = cfg.news_recent_weight
    na = ai.news_analysis
    return round(w * na.recent_catalysts_score + (1 - w) * na.historical_baseline_score, 1)
