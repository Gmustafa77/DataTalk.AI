"""Supervisor agent — decides how every chat question is answered (no LLM calls itself).

Routing order for one question:

1. **Input check**     — empty / too short / too long → polite rejection (0 LLM calls).
2. **Fast path**       — deterministic quick action on the in-memory dataset (0 LLM calls).
3. **Configuration**   — no ``GROQ_API_KEY`` → explanatory message (0 LLM calls).
4. **Question-answer cache** — exact normalised match with matching fingerprints → stored
   answer (0 LLM calls).
5. **Analytics engine** — planner → SQL → insight → synthesis/validation (see README).
6. **Direct-SQL fallback** — only if the engine raised unexpectedly: one SQL generation
   (+0–1 repair) and the composer narrative (0–1).

Every AI run is wrapped in :func:`core.llm.trace_calls`; the per-question call
accounting is returned in ``AssistantAnswer.meta``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd

import prompts as P
from agents.analytics_reasoning_agent import AnalyticsEngine, PipelineResult
from agents.insight_generation_agent import COMPOSER_VERSION, compose_insight
from agents.visualization_agent import ChartSource, fallback_charts
from core.fast_paths import match_fast_path, run_fast_path
from core.llm import trace_calls
from core.plot_style import style_figure
from core.qa_cache import QACache, fingerprint, normalize_question, question_may_contain_pii
from core.schemas import AssistantAnswer
from core.settings import SENSITIVE_COLUMNS, Settings
from core.sql_guard import projects_sensitive_columns

log = logging.getLogger("datatalk.supervisor")

MAX_QUESTION_CHARS = 2000


@dataclass
class QuestionContext:
    """Everything about the current session the supervisor needs to answer a question."""

    base_df: pd.DataFrame            # full loaded dataset (fast paths run on this)
    available_values: dict           # grounding values for the planner / SQL agent
    data_fingerprint: str
    conversation: list[str]          # recent chat messages (planner uses the last three)


class Supervisor:
    def __init__(self, settings: Settings, engine: AnalyticsEngine, cache: QACache | None) -> None:
        self.settings = settings
        self.engine = engine
        self.cache = cache if settings.qa_cache_enabled else None
        self.ai_fingerprint = settings.ai_fingerprint(P.PROMPT_VERSION, COMPOSER_VERSION)

    # ── entry point ──────────────────────────────────────────────────────────────
    def answer(self, question: str, ctx: QuestionContext, status_fn: Callable[[str], None]) -> AssistantAnswer:
        t0 = time.perf_counter()
        q = " ".join((question or "").split())
        if len(q) < 3:
            return self._done(AssistantAnswer(text="Please provide a more specific Appeals question.",
                                              route="rejected"), t0)
        if len(q) > MAX_QUESTION_CHARS:
            return self._done(AssistantAnswer(text=f"Please shorten the question to under {MAX_QUESTION_CHARS} "
                                                   "characters.", route="rejected"), t0)

        match = match_fast_path(q, ctx.base_df)
        if match is not None:
            status_fn("⚡ Running a deterministic quick action…")
            answer = run_fast_path(match, ctx.base_df, status_fn)
            self._ensure_chart(answer, q)
            answer.meta.update(fast_path=match.name)
            return self._done(answer, t0)

        if not self.settings.llm_configured:
            return self._done(AssistantAnswer(
                text="⚠️ The AI assistant is not configured: set `GROQ_API_KEY` in `.env` and restart the app. "
                     "Quick actions and dashboards keep working.", route="rejected"), t0)

        qnorm = normalize_question(q)
        recent = ctx.conversation[-3:]
        prints = {"ai": self.ai_fingerprint, "data": ctx.data_fingerprint,
                  "context": fingerprint(recent), "as_of_date": date.today().isoformat()}
        if self.cache is None:
            return self._done(self._run_ai(q, ctx, recent, status_fn, cache_note="disabled"), t0)

        key = self.cache.make_key(qnorm, **prints)
        with self.cache.inflight(key):
            hit = self.cache.get(key)
            if hit is not None:
                status_fn("⚡ Answer served from the question-answer cache (0 AI calls).")
                hit.filtered_df = ctx.base_df
                hit.meta.update(llm_calls=0, llm_requests=0, calls_by_purpose={})
                return self._done(hit, t0)
            answer = self._run_ai(q, ctx, recent, status_fn, cache_note="miss")
            if answer.cacheable:
                if question_may_contain_pii(q):
                    answer.meta["cache"] = "bypass: question contains an identifier"
                elif self.cache.put(key, question_norm=qnorm, answer=answer, model=self.settings.groq_model,
                                    prompt_version=P.PROMPT_VERSION, fingerprints=prints,
                                    llm_calls=answer.meta.get("llm_calls", 0)):
                    answer.meta["cache"] = "stored"
        return self._done(answer, t0)

    # ── AI workflow ──────────────────────────────────────────────────────────────
    def _run_ai(self, q: str, ctx: QuestionContext, recent: list[str], status_fn: Callable[[str], None],
                cache_note: str) -> AssistantAnswer:
        with trace_calls() as trace:
            try:
                result = self.engine.run(q, available_values=ctx.available_values, status_fn=status_fn,
                                         conversation_context=recent)
                answer = self._from_pipeline(result, ctx)
            except Exception as exc:  # unexpected engine failure → degrade, never crash the chat
                log.exception("analytics engine failed; using direct-SQL fallback")
                status_fn("Primary pipeline unavailable; using the direct query path…")
                answer = self._direct_sql_fallback(q, ctx, type(exc).__name__)
        answer.meta.update(trace.summary())
        answer.meta.setdefault("cache", cache_note)
        self._ensure_chart(answer, q)
        return answer

    def _from_pipeline(self, result: PipelineResult, ctx: QuestionContext) -> AssistantAnswer:
        sensitive = any(
            o.contract is not None and (not o.contract.is_aggregate
                                        or projects_sensitive_columns(o.contract.sql, SENSITIVE_COLUMNS))
            for o in result.outcomes)
        meta = {"plan_status": result.plan_status, "question_count": result.question_count,
                "timings": result.timings, "validation": {k: v for k, v in result.validation.items()
                                                          if k in ("verdict", "unanswered_ids",
                                                                   "dropped_numeric_claims", "charts_reconciled")},
                "defects": len(result.validation.get("defects", []))}
        if sensitive:
            meta["cache"] = "bypass: row-level or person-level result"
        return AssistantAnswer(text=result.text, table=result.primary_df, figures=result.figures,
                               insight=result.insight_card or None, filtered_df=ctx.base_df,
                               route="analytics_engine", cacheable=result.fully_answered and not sensitive,
                               meta=meta)

    def _direct_sql_fallback(self, q: str, ctx: QuestionContext, error_type: str) -> AssistantAnswer:
        sub = {"id": "q1", "question_text": q, "answerable": True, "analysis_type": "metric", "depends_on": None}
        out = self.engine.sql_agent.generate(sub, as_of_date=date.today().isoformat(),
                                             available_values=ctx.available_values)
        meta = {"engine_error": error_type}
        if not out.ok:
            return AssistantAnswer(text=out.failure_reason or "The query could not be completed.",
                                   route="direct_sql_fallback", meta=meta)
        try:
            df, _ = self.engine.run_sql(out.check.sql)
        except Exception as exc:
            log.warning("fallback sql execution failed: %s", type(exc).__name__)
            return AssistantAnswer(text=f"The generated SQL could not be executed: {str(exc)[:300]}",
                                   route="direct_sql_fallback", meta=meta)
        if df.empty:
            return AssistantAnswer(text="No records matched the criteria. Verify names, statuses and date ranges.",
                                   table=df, route="direct_sql_fallback", meta=meta)
        insight = compose_insight(df, question=q, sql=out.check.sql, config=self.engine.composer_cfg,
                                  llm=self.engine.llm if self.settings.insight_llm_narrative else None)
        insight["_sql"] = out.check.sql
        figures, _ = self.engine.charts.build(ChartSource("q1", df, ""))
        return AssistantAnswer(text=insight.get("insight", ""), table=df, figures=figures, insight=insight,
                               filtered_df=ctx.base_df, route="direct_sql_fallback", meta=meta)

    # ── helpers ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _ensure_chart(answer: AssistantAnswer, question: str) -> None:
        """Add a fallback chart when a table arrived without figures."""
        if answer.figures or answer.table is None or answer.table.empty:
            return
        answer.figures = fallback_charts(answer.table, question, style_fn=lambda f: style_figure(f, 440))
        if not answer.figures:
            answer.text += ("\n\n_No reliable chart type was available for this result shape; "
                            "the verified table is shown instead._")

    @staticmethod
    def _done(answer: AssistantAnswer, t0: float) -> AssistantAnswer:
        answer.meta.setdefault("llm_calls", 0)
        answer.meta["route"] = answer.route
        answer.meta["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return answer
