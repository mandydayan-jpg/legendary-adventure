"""Streamlit dashboard for the Pre-Breakout Squeeze Scanner.

Run:  streamlit run frontend/app.py
Talks only to the FastAPI backend (BACKEND_URL, default http://localhost:8000).
"""
from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

st.set_page_config(page_title="Squeeze Scanner", page_icon="📈", layout="wide")

REC_BADGE = {"BUY_WATCHLIST": "🟢 BUY_WATCHLIST", "WAIT": "🟡 WAIT", "REJECT": "🔴 REJECT"}
STATUS_BADGE = {"OK": "✅ verified", "VALIDATION_FAILED": "⛔ validation failed",
                "AI_ERROR": "⚠️ AI error", "DATA_ERROR": "⚠️ data error"}

# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    backend = st.text_input("Backend URL", os.getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    try:
        h = requests.get(f"{backend}/health", timeout=5).json()
        st.success(f"Backend OK · model `{h['model']}`")
        if not h["anthropic_key_configured"]:
            st.warning("ANTHROPIC_API_KEY is not set on the backend.")
        st.caption("News providers: " + ", ".join(h["news_providers"]))
    except Exception as exc:
        st.error(f"Backend unreachable: {exc}")
        st.stop()

    st.subheader("Scan")
    tickers_txt = st.text_area("Tickers (comma separated, empty = default universe)", "")
    min_cond = st.slider("Technical rules required before AI analysis", 0, 3, 3)
    max_ai = st.number_input("Max Claude calls", 0, 100, 10)
    run = st.button("🔍 Run scan", type="primary", width="stretch")

    st.subheader("Single ticker")
    single = st.text_input("Ticker", "")
    run_single = st.button("Analyze ticker", width="stretch")

    with st.expander("Rulebook"):
        for k, v in requests.get(f"{backend}/rules", timeout=5).json().items():
            st.markdown(f"**{k}**: {v}")

st.title("📈 Pre-Breakout Squeeze Scanner")
st.caption("Real market data + real news only. Every AI answer is validated by the server before it is shown.")

if run:
    body = {"tickers": [t.strip() for t in tickers_txt.split(",") if t.strip()] or None,
            "min_conditions": min_cond, "analyze": True, "max_ai_calls": int(max_ai)}
    with st.spinner("Scanning market data, collecting news and running Claude analysis..."):
        r = requests.post(f"{backend}/scan", json=body, timeout=1800)
    if r.ok:
        st.session_state["scan"] = r.json()
    else:
        st.error(f"Scan failed: {r.status_code} {r.text}")

if run_single and single.strip():
    with st.spinner(f"Analyzing {single.upper()}..."):
        r = requests.get(f"{backend}/analyze/{single.strip()}", timeout=600)
    if r.ok:
        st.session_state["single"] = r.json()
    else:
        st.error(f"Analysis failed: {r.status_code} {r.text}")


# ----------------------------------------------------------------------------- helpers
@st.cache_data(ttl=300, show_spinner=False)
def load_history(url: str, ticker: str) -> list[dict]:
    r = requests.get(f"{url}/history/{ticker}", params={"days": 90}, timeout=60)
    r.raise_for_status()
    return r.json()["bars"]


def chart(ticker: str, tech: dict | None, entry: float | None, stop: float | None):
    try:
        bars = pd.DataFrame(load_history(backend, ticker))
    except Exception as exc:
        st.info(f"Chart unavailable: {exc}")
        return
    fig = go.Figure(go.Candlestick(x=bars["date"], open=bars["open"], high=bars["high"],
                                   low=bars["low"], close=bars["close"], name=ticker))
    if entry:
        fig.add_hline(y=entry, line_color="#16a34a", line_dash="dash", annotation_text=f"Trigger {entry:.2f}")
    if stop:
        fig.add_hline(y=stop, line_color="#dc2626", line_dash="dash", annotation_text=f"Stop {stop:.2f}")
    if tech:
        fig.add_hline(y=tech["high_30d"], line_color="#6b7280", line_dash="dot", annotation_text="30d high",
                      annotation_position="bottom right")
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), xaxis_rangeslider_visible=False)
    st.plotly_chart(fig, width="stretch")


def render_result(res: dict):
    a, v, tech, news = res.get("analysis"), res.get("validation"), res.get("technicals"), res.get("news")
    st.markdown(f"**Status:** {STATUS_BADGE.get(res['status'], res['status'])} · model `{res.get('model')}` · "
                f"analyzed {res['analyzed_at_utc']}")
    if res.get("error"):
        st.error(res["error"])

    if v:
        for e in v["errors"]:
            st.error(f"Validation: {e}")
        for k, o in v["overrides"].items():
            st.warning(f"Server override `{k}`: {o}")
        for w in v["warnings"]:
            st.info(f"Warning: {w}")

    if a:
        c = st.columns(5)
        c[0].metric("Recommendation", a["recommendation"])
        c[1].metric("Confidence", a["confidence_score"])
        c[2].metric("Entry trigger", f"{a['entry_trigger_price']:.2f}")
        c[3].metric("Stop-loss", f"{a['stop_loss_price']:.2f}")
        risk = (a["entry_trigger_price"] - a["stop_loss_price"]) / a["entry_trigger_price"]
        c[4].metric("Risk to stop", f"{risk:.1%}")

        st.markdown("#### Conditions")
        for name, cond in a["conditions_check"].items():
            st.markdown(f"{'✅' if cond['met'] else '❌'} **{name}** — {cond['evidence']}")
            for cit in cond.get("citations", []) or []:
                art = next((x for x in (news or {}).get("articles", []) if x["id"] == cit["article_id"]), None)
                link = f" · [{art['publisher'] or 'source'}]({art['url']})" if art and art.get("url") else ""
                st.markdown(f"> “{cit['quote']}” — {cit['date']} ({cit['article_id']}){link}")

        na = a["news_analysis"]
        st.markdown("#### News analysis")
        c = st.columns(4)
        c[0].metric("Recent (0-72h)", na["recent_catalysts_score"])
        c[1].metric("Historical", na["historical_baseline_score"])
        c[2].metric("Time-weighted (70/30)", res.get("time_weighted_news_score"))
        c[3].metric("Sentiment", na["time_weighted_sentiment"])
        st.markdown(f"**Key recent event:** {na['key_recent_event']}")
        st.markdown(f"**Key historical event:** {na['key_historical_event']}")
        st.markdown("#### Reasoning")
        st.write(a["summary_reasoning"])

    # tabs instead of expanders: Streamlit does not allow nested expanders
    t_chart, t_tech, t_news = st.tabs(["Chart", "Raw technical data (ground truth)", "News used"])
    with t_chart:
        chart(res["ticker"], tech, a["entry_trigger_price"] if a else None, a["stop_loss_price"] if a else None)
    with t_tech:
        st.json(tech or {})
    with t_news:
        if news:
            st.caption(f"{news['counts']['total']} articles · buckets {news['counts']}")
            if news.get("provider_errors"):
                st.warning(f"Provider errors: {news['provider_errors']}")
            if news["articles"]:
                st.dataframe(pd.DataFrame(news["articles"])[
                    ["id", "published_date", "bucket", "weight", "publisher", "title", "url"]],
                    width="stretch", hide_index=True)


# ----------------------------------------------------------------------------- main view
if "single" in st.session_state:
    s = st.session_state["single"]
    st.subheader(f"Single analysis: {s['ticker']}")
    render_result(s)
    st.divider()

scan = st.session_state.get("scan")
if not scan:
    st.info("Run a scan from the sidebar.")
    st.stop()

c = st.columns(4)
c[0].metric("Tickers scanned", f"{scan['scanned_ok']}/{scan['requested']}")
c[1].metric("Passed pre-filter", len(scan["passed_prefilter"]))
c[2].metric("AI analyzed", len(scan["results"]))
c[3].metric("Duration", f"{scan['duration_sec']}s")
st.caption(f"Scan time (UTC): {scan['scanned_at_utc']}")
if scan["skipped_ai_due_to_limit"]:
    st.warning("Not analyzed (limit): " + ", ".join(scan["skipped_ai_due_to_limit"]))

tab_res, tab_pre, tab_err = st.tabs(["AI results", "Technical pre-filter", "Data errors"])

with tab_res:
    rows = []
    for r in scan["results"]:
        a = r.get("analysis") or {}
        rows.append({
            "Ticker": r["ticker"],
            "Recommendation": REC_BADGE.get(a.get("recommendation"), "—"),
            "Confidence": a.get("confidence_score"),
            "Entry trigger": a.get("entry_trigger_price"),
            "Stop-loss": a.get("stop_loss_price"),
            "News score": r.get("time_weighted_news_score"),
            "Status": STATUS_BADGE.get(r["status"], r["status"]),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        for r in scan["results"]:
            a = r.get("analysis") or {}
            with st.expander(f"{r['ticker']} · {REC_BADGE.get(a.get('recommendation'), r['status'])}"):
                render_result(r)
    else:
        st.info("No ticker passed the technical pre-filter, so Claude was not called.")

with tab_pre:
    snaps = pd.DataFrame(scan["technical_snapshots"])
    if not snaps.empty:
        cols = ["ticker", "as_of", "last_bar_partial", "current_price", "high_30d", "compression_ratio",
                "volume_ratio", "distance_from_high_pct", "price_compression", "volume_dry_up",
                "near_resistance", "conditions_met"]
        st.dataframe(snaps[cols], width="stretch", hide_index=True)

with tab_err:
    if scan["data_errors"]:
        st.table(pd.DataFrame(scan["data_errors"].items(), columns=["Ticker", "Error"]))
    else:
        st.success("No data errors.")
