"""News / catalyst collection with time weighting.

Sources (all real, all optional except yfinance):
  * Yahoo Finance via yfinance (no key)          - always on
  * Finnhub company-news   (FINNHUB_API_KEY)     - best 30-day coverage on the free tier
  * Polygon reference news (POLYGON_API_KEY)
  * Alpha Vantage NEWS_SENTIMENT (ALPHAVANTAGE_API_KEY)

Time weighting (rulebook):
  * RECENT_0_72H   : articles from the last 72 hours share 70% of the total weight.
  * CRITICAL_3_5D  : 72h - 5 days, elevated per-article weight inside the remaining 30%.
  * BASELINE_5_30D : 5 - 30 days, historical baseline.
Every article gets a stable id (N1, N2, ...) so the AI must cite by id + verbatim quote + date,
and the validator can check each citation against this exact list.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import Settings, settings

log = logging.getLogger(__name__)

BUCKET_RECENT = "RECENT_0_72H"
BUCKET_CRITICAL = "CRITICAL_3_5D"
BUCKET_BASELINE = "BASELINE_5_30D"


@dataclass
class NewsArticle:
    id: str
    provider: str           # yfinance / finnhub / polygon
    publisher: str
    title: str
    summary: str
    url: str
    published_utc: str      # ISO-8601
    published_date: str     # YYYY-MM-DD (UTC) - the date the AI must cite
    age_hours: float
    bucket: str
    weight: float           # share of total news weight (sums to <= 1.0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NewsBundle:
    ticker: str
    fetched_at_utc: str
    lookback_days: int
    articles: list[NewsArticle]
    provider_errors: dict[str, str]
    counts: dict[str, int]
    bucket_weights: dict[str, float]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["articles"] = [a.to_dict() for a in self.articles]
        return d

    def by_id(self) -> dict[str, NewsArticle]:
        return {a.id: a for a in self.articles}


# --------------------------------------------------------------------------- providers
def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _from_yfinance(ticker: str) -> list[dict]:
    import yfinance as yf

    raw = yf.Ticker(ticker).get_news(count=50) or []
    out = []
    for item in raw:
        c = item.get("content") or item  # newer yfinance nests everything under "content"
        title = c.get("title")
        pub = c.get("pubDate") or c.get("displayTime")
        ts = None
        if pub:
            ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
        elif item.get("providerPublishTime"):
            ts = datetime.fromtimestamp(int(item["providerPublishTime"]), tz=timezone.utc)
        if not title or ts is None:
            continue
        provider = c.get("provider") or {}
        url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or item.get("link", "")
        out.append({
            "provider": "yfinance",
            "publisher": provider.get("displayName") if isinstance(provider, dict) else (item.get("publisher") or ""),
            "title": title,
            "summary": c.get("summary") or c.get("description") or "",
            "url": url or "",
            "ts": ts,
        })
    return out


def _from_finnhub(ticker: str, since: datetime, until: datetime, cfg: Settings) -> list[dict]:
    r = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": ticker, "from": since.date().isoformat(), "to": until.date().isoformat(),
                "token": cfg.finnhub_api_key},
        timeout=cfg.http_timeout,
    )
    r.raise_for_status()
    out = []
    for it in r.json() or []:
        if not it.get("headline") or not it.get("datetime"):
            continue
        out.append({
            "provider": "finnhub",
            "publisher": it.get("source") or "",
            "title": it["headline"],
            "summary": it.get("summary") or "",
            "url": it.get("url") or "",
            "ts": datetime.fromtimestamp(int(it["datetime"]), tz=timezone.utc),
        })
    return out


def _from_polygon(ticker: str, since: datetime, cfg: Settings) -> list[dict]:
    r = requests.get(
        "https://api.polygon.io/v2/reference/news",
        params={"ticker": ticker, "published_utc.gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "order": "desc", "limit": 100, "apiKey": cfg.polygon_api_key},
        timeout=cfg.http_timeout,
    )
    r.raise_for_status()
    out = []
    for it in (r.json() or {}).get("results", []):
        if not it.get("title") or not it.get("published_utc"):
            continue
        out.append({
            "provider": "polygon",
            "publisher": (it.get("publisher") or {}).get("name", ""),
            "title": it["title"],
            "summary": it.get("description") or "",
            "url": it.get("article_url") or "",
            "ts": datetime.fromisoformat(it["published_utc"].replace("Z", "+00:00")),
        })
    return out


def _from_alphavantage(ticker: str, since: datetime, cfg: Settings) -> list[dict]:
    r = requests.get(
        "https://www.alphavantage.co/query",
        params={"function": "NEWS_SENTIMENT", "tickers": ticker, "time_from": since.strftime("%Y%m%dT%H%M"),
                "sort": "LATEST", "limit": 200, "apikey": cfg.alphavantage_api_key},
        timeout=cfg.http_timeout,
    )
    r.raise_for_status()
    js = r.json()
    if "feed" not in js:  # errors / rate limits come back as HTTP 200 with a message key
        raise RuntimeError(str(js.get("Information") or js.get("Note") or js.get("Error Message") or js)[:200])
    out = []
    for it in js["feed"]:
        if not it.get("title") or not it.get("time_published"):
            continue
        out.append({
            "provider": "alphavantage",
            "publisher": it.get("source") or "",
            "title": it["title"],
            "summary": it.get("summary") or "",
            "url": it.get("url") or "",
            # "20261030T153000" - Alpha Vantage publishes times in US/Eastern
            "ts": datetime.strptime(it["time_published"][:15], "%Y%m%dT%H%M%S")
                  .replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc),
        })
    return out


# --------------------------------------------------------------------------- weighting
def bucket_for(age_hours: float, cfg: Settings = settings) -> str:
    if age_hours <= cfg.news_recent_hours:
        return BUCKET_RECENT
    if age_hours <= cfg.news_critical_days * 24:
        return BUCKET_CRITICAL
    return BUCKET_BASELINE


def assign_weights(articles: list[NewsArticle], cfg: Settings = settings) -> dict[str, float]:
    """Give each article a share of the total news weight.

    RECENT bucket owns `news_recent_weight` (70%). The remaining 30% is split between
    CRITICAL (2/3) and BASELINE (1/3). If a bucket is empty its share is NOT redistributed:
    missing recent news must lower the score, not be papered over.
    Inside a bucket, newer articles weigh more (exponential decay, half-life 24h / 72h / 7d).
    """
    rest = 1.0 - cfg.news_recent_weight
    shares = {BUCKET_RECENT: cfg.news_recent_weight, BUCKET_CRITICAL: rest * 2 / 3, BUCKET_BASELINE: rest / 3}
    half_life = {BUCKET_RECENT: 24.0, BUCKET_CRITICAL: 72.0, BUCKET_BASELINE: 168.0}
    for bucket, share in shares.items():
        members = [a for a in articles if a.bucket == bucket]
        raw = [math.pow(0.5, a.age_hours / half_life[bucket]) for a in members]
        total = sum(raw)
        for a, w in zip(members, raw):
            a.weight = round(share * w / total, 5) if total else 0.0
    return {k: round(v, 4) for k, v in shares.items()}


def _dedupe_key(title: str) -> str:
    norm = re.sub(r"[^a-z0-9 ]", "", title.lower())
    return hashlib.sha1(norm.encode()).hexdigest()


# --------------------------------------------------------------------------- public API
def collect_news(ticker: str, cfg: Settings = settings, now: datetime | None = None) -> NewsBundle:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=cfg.news_lookback_days)
    errors: dict[str, str] = {}
    raw: list[dict] = []

    providers = [("yfinance", lambda: _from_yfinance(ticker))]
    if cfg.finnhub_api_key:
        providers.append(("finnhub", lambda: _from_finnhub(ticker, since, now, cfg)))
    if cfg.polygon_api_key:
        providers.append(("polygon", lambda: _from_polygon(ticker, since, cfg)))
    if cfg.alphavantage_api_key:
        providers.append(("alphavantage", lambda: _from_alphavantage(ticker, since, cfg)))

    for name, fn in providers:
        try:
            raw.extend(fn())
        except Exception as exc:
            log.warning("news provider %s failed for %s: %s", name, ticker, exc)
            errors[name] = str(exc)[:300]

    seen: set[str] = set()
    articles: list[NewsArticle] = []
    for it in sorted(raw, key=lambda x: x["ts"], reverse=True):
        ts: datetime = it["ts"].astimezone(timezone.utc)
        if ts < since or ts > now + timedelta(minutes=5):
            continue  # outside the 30-day window (or bogus future timestamp)
        key = _dedupe_key(it["title"])
        if key in seen:
            continue
        seen.add(key)
        age = max((now - ts).total_seconds() / 3600, 0.0)
        articles.append(NewsArticle(
            id="",  # assigned below
            provider=it["provider"],
            publisher=_clean(it["publisher"]),
            title=_clean(it["title"]),
            summary=_clean(it["summary"])[:1200],
            url=it["url"],
            published_utc=ts.isoformat(timespec="seconds"),
            published_date=ts.date().isoformat(),
            age_hours=round(age, 1),
            bucket=bucket_for(age, cfg),
            weight=0.0,
        ))

    for i, a in enumerate(articles, start=1):
        a.id = f"N{i}"
    shares = assign_weights(articles, cfg)
    counts = {b: sum(1 for a in articles if a.bucket == b) for b in (BUCKET_RECENT, BUCKET_CRITICAL, BUCKET_BASELINE)}
    counts["total"] = len(articles)

    return NewsBundle(
        ticker=ticker,
        fetched_at_utc=now.isoformat(timespec="seconds"),
        lookback_days=cfg.news_lookback_days,
        articles=articles,
        provider_errors=errors,
        counts=counts,
        bucket_weights=shares,
    )
