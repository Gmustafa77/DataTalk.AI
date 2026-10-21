"""Persistent question-answer cache (``data/question_answer.json``).

Purpose: when an equivalent question has already been answered successfully, return
the stored answer without calling the LLM.

* **Key** — SHA-256 of the normalised question plus fingerprints of everything that
  can change the answer: AI configuration (model, prompt and composer versions,
  engine switches, business rules), the loaded data snapshot, the conversation
  context sent to the planner, and today's date (relative dates such as "last
  month" are resolved against it).
* **Writes** — whole-file rewrite to a temporary file followed by ``os.replace``
  under a cross-process ``filelock``; entries are pruned to ``max_entries``.
* **Reads** — the file is re-read when its modification time changes, so several
  Streamlit processes see each other's entries.
* **Concurrency** — :meth:`QACache.inflight` serialises identical in-process
  questions so a burst of the same question triggers one AI run.
* **Recovery** — a corrupt file is renamed to ``question_answer.corrupt-<ts>.json``
  and a fresh cache is started; an outdated ``cache_version`` is discarded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.io as pio
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, ValidationError

from core.schemas import AssistantAnswer

log = logging.getLogger("datatalk.qa_cache")

CACHE_VERSION = 1
_PII_RE = re.compile(r"\b\d{6,}\b|[\w.+-]+@[\w-]+\.[\w.]+")


class CacheEntry(BaseModel):
    """One persisted answer. ``answer`` holds JSON-safe text, table, figures and insight."""

    question: str
    created_at: str
    model: str
    prompt_version: str
    fingerprints: dict[str, str]
    llm_calls_saved: int = Field(0, ge=0)
    answer: dict[str, Any]


def normalize_question(question: str) -> str:
    """Case-, whitespace- and trailing-punctuation-insensitive form used for cache keys."""
    text = unicodedata.normalize("NFKC", question or "").strip().lower()
    text = text.strip("\"'`“”‘’")
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"[\s?.!]+$", "", text)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def question_may_contain_pii(question: str) -> bool:
    """Long digit runs (IDs) or e-mail addresses → never persist."""
    return bool(_PII_RE.search(question or ""))


# ── JSON conversion ────────────────────────────────────────────────────────────
def to_jsonable(obj: Any) -> Any:
    """Recursively convert numpy / pandas / NaN values into strict-JSON values."""
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items() if not isinstance(v, pd.DataFrame)}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    if obj is pd.NaT:
        return None
    return str(obj)


def serialize_answer(answer: AssistantAnswer) -> dict[str, Any]:
    table = None
    if answer.table is not None:
        split = json.loads(answer.table.to_json(orient="split", date_format="iso", index=False))
        table = {"columns": split["columns"], "data": split["data"]}
    return {
        "text": answer.text,
        "table": table,
        "figures": [json.loads(pio.to_json(f, validate=False)) for f in answer.figures],
        "insight": to_jsonable(answer.insight) if answer.insight else None,
    }


def deserialize_answer(payload: dict[str, Any]) -> AssistantAnswer:
    table = payload.get("table")
    frame = pd.DataFrame(table["data"], columns=table["columns"]) if table else None
    figures = [pio.from_json(json.dumps(f), skip_invalid=True) for f in payload.get("figures") or []]
    return AssistantAnswer(text=payload["text"], table=frame, figures=figures, insight=payload.get("insight"),
                           route="cache")


# ═══════════════════════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════════════════════
class QACache:
    def __init__(self, path: Path, *, max_entries: int = 500, max_entry_kb: int = 768) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self.max_entry_bytes = max_entry_kb * 1024
        self._file_lock = FileLock(str(self.path) + ".lock", timeout=10)
        self._lock = threading.RLock()
        self._entries: dict[str, CacheEntry] = {}
        self._mtime: float | None = None
        self._inflight: dict[str, threading.Lock] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(question_norm: str, **fingerprints: str) -> str:
        return hashlib.sha256(json.dumps({"q": question_norm, **fingerprints}, sort_keys=True).encode()).hexdigest()

    # ── file I/O ─────────────────────────────────────────────────────────────────
    def _read_file(self) -> dict[str, CacheEntry]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{datetime.now():%Y%m%d%H%M%S}.json")
            log.warning("qa_cache: corrupt file moved to %s; starting empty", backup.name)
            os.replace(self.path, backup)
            return {}
        if not isinstance(raw, dict) or raw.get("cache_version") != CACHE_VERSION:
            log.info("qa_cache: cache_version mismatch; discarding stored entries")
            return {}
        entries: dict[str, CacheEntry] = {}
        for key, value in (raw.get("entries") or {}).items():
            try:
                entries[key] = CacheEntry.model_validate(value)
            except ValidationError:
                log.warning("qa_cache: dropped one invalid entry")
        return entries

    def _refresh(self) -> None:
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self._mtime:
            self._entries = self._read_file()
            self._mtime = self.path.stat().st_mtime if self.path.exists() else None

    def _write_file(self, entries: dict[str, CacheEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"cache_version": CACHE_VERSION, "updated_at": datetime.now(UTC).isoformat(),
               "entries": {k: v.model_dump(mode="json") for k, v in entries.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        for attempt in range(5):  # Windows may briefly lock the target while another process reads it
            try:
                os.replace(tmp, self.path)
                break
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        else:
            raise PermissionError(f"could not replace {self.path}")
        self._mtime = self.path.stat().st_mtime

    # ── public API ───────────────────────────────────────────────────────────────
    def get(self, key: str) -> AssistantAnswer | None:
        with self._lock:
            try:
                self._refresh()
            except OSError as exc:
                log.warning("qa_cache: read failed (%s); treating as miss", type(exc).__name__)
                self.misses += 1
                return None
            entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        try:
            answer = deserialize_answer(entry.answer)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("qa_cache: entry could not be restored (%s); treating as miss", type(exc).__name__)
            self.misses += 1
            return None
        self.hits += 1
        answer.meta = {"cache": "hit", "cached_at": entry.created_at, "llm_calls_saved": entry.llm_calls_saved}
        return answer

    def put(self, key: str, *, question_norm: str, answer: AssistantAnswer, model: str, prompt_version: str,
            fingerprints: dict[str, str], llm_calls: int) -> bool:
        """Persist a validated answer; returns ``False`` when it is too large or the write fails."""
        payload = serialize_answer(answer)
        entry = CacheEntry(question=question_norm, created_at=datetime.now(UTC).isoformat(), model=model,
                           prompt_version=prompt_version, fingerprints=fingerprints, llm_calls_saved=llm_calls,
                           answer=payload)
        if len(json.dumps(payload, default=str)) > self.max_entry_bytes:
            log.info("qa_cache: answer larger than max entry size; not cached")
            return False
        with self._lock:
            try:
                with self._file_lock:
                    entries = self._read_file()
                    entries[key] = entry
                    if len(entries) > self.max_entries:
                        oldest = sorted(entries, key=lambda k: entries[k].created_at)
                        for k in oldest[: len(entries) - self.max_entries]:
                            entries.pop(k, None)
                    self._write_file(entries)
                    self._entries = entries
            except (OSError, Timeout, ValueError) as exc:
                log.warning("qa_cache: write failed (%s); answer not cached", type(exc).__name__)
                return False
        return True

    @contextmanager
    def inflight(self, key: str) -> Iterator[None]:
        """Serialise concurrent processing of the same key inside this process."""
        with self._lock:
            lock = self._inflight.setdefault(key, threading.Lock())
        with lock:
            yield

    def clear(self) -> None:
        with self._lock, self._file_lock:
            self._write_file({})
            self._entries = {}

    def stats(self) -> dict[str, int]:
        with self._lock:
            try:
                self._refresh()
            except OSError:
                pass
            return {"entries": len(self._entries), "hits": self.hits, "misses": self.misses}
