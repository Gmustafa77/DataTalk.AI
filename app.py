"""DataTalk.AI — Streamlit entry point.

Responsibilities: page configuration, theme CSS, data loading, the shared header
and KPI strip, sidebar status, page navigation and the footer. All features live
in ``dashboards/``, ``components/``, ``agents/`` and ``core/``.

Run with:  uv run streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from anomaly_detection import detect_anomalies
from components import ui
from components.risk_findings import RiskConfig, render_risk_findings_tab
from core import runtime
from core.data import KPI_META, calculate_kpis, fmt_kpi, kpi_delta
from core.fast_paths import ANOMALY_CONFIG
from core.settings import AGING_THRESHOLD, ESCALATION_LEVEL, SLA_DAYS, get_settings
from dashboards.ai_assistant import render_ai_assistant
from dashboards.data_explorer import render_data_explorer
from dashboards.deep_dive_dashboard import render_deep_dive_tab
from dashboards.intelligence_dashboard import render_intelligence_page

st.set_page_config(page_title="DataTalk.AI • EXL", page_icon="🧠", layout="wide",
                   initial_sidebar_state="collapsed")

settings = get_settings()
runtime.configure_logging(settings)
ui.inject_css()


# ── data ───────────────────────────────────────────────────────────────────────
try:
    with st.spinner("Connecting to Databricks and loading appeals data…"):
        base_df = runtime.load_dataset()
except Exception as exc:  # configuration or connectivity problem: explain and stop
    runtime.log.exception("data load failed")
    st.error(f"❌ Could not load the appeals data: {exc}", icon="🔴")
    st.info("Check the DATABRICKS_* settings in `.env` (see README → Configuration Reference).")
    ui.footer()
    st.stop()
if base_df.empty:
    st.warning("The appeals table returned no rows.")
    ui.footer()
    st.stop()

runtime.init_session(base_df)
current_df: pd.DataFrame = st.session_state["dashboard_df"]


# ── header ─────────────────────────────────────────────────────────────────────
def render_header() -> None:
    t = st.session_state["telemetry"]
    db_status, db_ms = runtime.ping_databricks()
    lat = t["latencies_ms"]
    pills = "".join([
        ui.pill("🟢" if db_status == "connected" else "🔴" if db_status == "error" else "⚪", "DB",
                f"{db_ms:.0f} ms" if db_status == "connected" else db_status, "Databricks round trip (SELECT 1), cached 60 s"),
        ui.pill("🧠", "LLM", settings.groq_model if settings.llm_configured else "not configured", "Groq model"),
        ui.pill("🤖", "AI calls", f"{t['llm_calls']}", f"{t['llm_requests']} provider request(s) this session"),
        ui.pill("🪙", "Tokens", f"{t['tokens_in'] + t['tokens_out']:,}", f"{t['tokens_in']:,} in · {t['tokens_out']:,} out"),
        ui.pill("💰", "Est. cost", f"${t['cost_usd']:.4f}", "Estimate from the price table in core/settings.py"),
        ui.pill("⚡", "Cache hits", f"{t['cache_hits']}/{t['questions']}", "Answers served from data/question_answer.json"),
        ui.pill("⏱️", "Avg answer", f"{(sum(lat) / len(lat) / 1000) if lat else 0:.1f}s", f"Across the last {len(lat)} answer(s)"),
        ui.pill("🛡️", "Blocked claims", f"{t['dropped_claims']}", "AI sentences removed because a number was not in the evidence"),
    ])
    ui.header(pills)


def render_kpis() -> None:
    baseline, active = calculate_kpis(base_df), calculate_kpis(current_df)
    cards = []
    for label, (key, icon) in KPI_META.items():
        delta, good = kpi_delta(label, active[key], baseline)
        cards.append(ui.kpi_card(icon, label, fmt_kpi(label, active[key]), fmt_kpi(label, baseline[key]), delta, good))
    for col, card in zip(st.columns(len(cards)), cards):
        col.markdown(card, unsafe_allow_html=True)
    if len(current_df) < len(base_df):
        c1, c2 = st.columns([5, 1], vertical_alignment="center")
        c1.info(f"🎯 Dashboard filtered by the last quick action — showing **{len(current_df):,}** of "
                f"**{len(base_df):,}** appeals.")
        if c2.button("Show all appeals", width="stretch"):
            st.session_state["dashboard_df"] = base_df
            st.rerun()


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("### ⚙️ Session")
        st.caption("Light / dark mode: ⋮ menu → **Settings** → Theme.")
        if st.button("🔄 Refresh data", width="stretch", help="Reload the appeals table from Databricks"):
            runtime.refresh_data()
            st.session_state.pop("dashboard_df", None)
            st.rerun()
        stats = runtime.get_qa_cache().stats() if settings.qa_cache_enabled else None
        st.markdown("### 💾 Question-answer cache")
        if stats is None:
            st.caption("Disabled (QA_CACHE_ENABLED=false).")
        else:
            st.caption(f"{stats['entries']} stored answer(s) · {stats['hits']} hit(s) · {stats['misses']} miss(es) "
                       "in this server process.")
            if st.button("🧹 Clear cached answers", width="stretch"):
                runtime.get_qa_cache().clear()
                st.toast("Question-answer cache cleared.")
        st.markdown("### ℹ️ Business rules")
        st.caption(f"SLA {SLA_DAYS} days · aging threshold {AGING_THRESHOLD} days · escalation level {ESCALATION_LEVEL}")


# ── pages ──────────────────────────────────────────────────────────────────────
def assistant_page() -> None:
    render_ai_assistant(base_df)


def deep_dive_page() -> None:
    render_deep_dive_tab(current_df)


def intelligence_page() -> None:
    render_intelligence_page(current_df)


def risk_page() -> None:
    render_risk_findings_tab(
        current_df, RiskConfig(sla_days=SLA_DAYS, escalation_level=ESCALATION_LEVEL, aging_threshold=AGING_THRESHOLD),
        detect_anomalies_fn=lambda d: detect_anomalies(d, ANOMALY_CONFIG),
    )


def explorer_page() -> None:
    render_data_explorer(current_df)


pages = [
    st.Page(assistant_page, title="AI Assistant", icon="🧠", url_path="assistant", default=True),
    st.Page(deep_dive_page, title="Deep Dive", icon="🔬", url_path="deep-dive"),
    st.Page(intelligence_page, title="Intelligence", icon="📋", url_path="intelligence"),
    st.Page(risk_page, title="Risk & Findings", icon="🏦", url_path="risk"),
    st.Page(explorer_page, title="Data Explorer", icon="📑", url_path="explorer"),
]
navigation = st.navigation(pages, position="top")

render_header()
render_kpis()
render_sidebar()
navigation.run()
ui.footer()
