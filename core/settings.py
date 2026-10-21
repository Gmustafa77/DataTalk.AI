"""Centralised configuration for DataTalk.AI.

Two kinds of configuration live here:

* ``Settings`` — runtime settings read from environment variables or a local
  ``.env`` file (pydantic-settings). Secrets are typed as ``SecretStr`` so they are
  never printed by accident.
* Business constants — the appeals rules (SLA, aging, escalation level) and the
  approved table schema. They are code, not environment, because changing them
  changes the meaning of every metric.

Precedence: real environment variables > ``.env`` file > defaults below.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Business rules (single source of truth) ────────────────────────────────────
SLA_DAYS = 30            # a case older than this is overdue
AGING_THRESHOLD = 60     # a case older than this is "aged"
ESCALATION_LEVEL = 2     # `Level Type Code` value that marks an escalation

# ── Approved schema ────────────────────────────────────────────────────────────
RAW_COLUMNS: tuple[str, ...] = (
    "Case Identifier", "Level Type Code", "Case Type Code",
    "Case Item Level Posted Date Timestamp", "Behalf Of",
    "Closed In Compliance Indicator", "Current Unit Name", "Current User Name",
    "Posted Date", "Posted Time", "Received Date", "Work Item Status Short Name",
    "Work Item Task Short Name", "Routed Timestamp",
    "Routed To Business Unit Name", "Routed To Dept Name", "Routed To Site Name",
    "Routed To User ID", "Routed To User Name", "Routed To User Segment Code",
    "Routed To User Segment Description", "Routing Comments Text",
    "Transferred By User Identifier", "Transferred By User Name",
    "Transferred To User Identifier", "Transferred To User Name",
    "Closed Date (CaseHistory)", "Case Decision Maker ID", "Pre Post Source Code",
    "ASO Grouping", "Plan Sponsor Name", "Determination", "Case Status",
    "Provider_ID", "Provider_Name", "Provider_Type", "Provider_Specialty",
    "Network_Status",
)

# Columns computed in pandas by ``core.data.enrich``. They do not exist in SQL.
DERIVED_COLUMNS: frozenset[str] = frozenset({
    "CaseAgeDays", "OpenAgingDays", "IsClosed", "IsOverdue", "IsEscalated",
    "IsTransferred", "IsCompliant", "ComplianceStatus", "ComplianceRiskScore",
    "ReceivedMonth", "ReceivedYear", "Quarter", "DeterminationLabel",
    "IsOverturned", "IsPartiallyOverturned", "IsUpheld", "IsAnyOverturn",
    "Provider ID", "Provider Name", "Provider Type", "Provider Specialty",
    "Network Status", "IsOutOfNetwork", "CaseTypeLabel", "BehalfOfLabel",
    "SourceLabel", "CaseStatusLabel",
})

# Person-level / free-text columns. Answers that project these columns row by row
# are never written to the persistent question-answer cache.
SENSITIVE_COLUMNS: frozenset[str] = frozenset({
    "Case Identifier", "Current User Name", "Routed To User ID", "Routed To User Name",
    "Transferred By User Identifier", "Transferred By User Name",
    "Transferred To User Identifier", "Transferred To User Name",
    "Case Decision Maker ID", "Routing Comments Text",
})

# Estimated USD per 1M tokens, used only for the session cost estimate in the UI.
LLM_PRICING_PER_1M: dict[str, dict[str, float]] = {
    "openai/gpt-oss-20b": {"input": 0.075, "output": 0.30},
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60},
}


class Settings(BaseSettings):
    """Runtime configuration. Every field maps to an upper-case environment variable."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
        case_sensitive=False,
    )

    # Databricks
    databricks_server_hostname: str = ""
    databricks_http_path: str = ""
    databricks_access_token: SecretStr = SecretStr("")
    databricks_catalog: str = ""
    databricks_schema: str = ""
    databricks_table: str = "appeals_final"
    data_cache_ttl_seconds: int = Field(0, ge=0, description="0 = keep loaded data until refreshed")

    # LLM (Groq)
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "openai/gpt-oss-20b"
    llm_temperature: float = Field(0.0, ge=0.0, le=1.0)
    llm_seed: int | None = 7
    llm_timeout_seconds: float = Field(45.0, gt=0)
    llm_max_attempts: int = Field(3, ge=1, le=5)
    llm_retry_base_seconds: float = Field(1.0, ge=0)
    llm_response_format: Literal["json_object", "json_schema"] = "json_object"

    # Query guard rails
    max_row_query_limit: int = Field(200, ge=1)
    max_result_rows: int = Field(5000, ge=1)
    sql_cache_ttl_seconds: int = Field(300, ge=0)
    sql_cache_max_entries: int = Field(64, ge=0)
    prompt_max_values_per_field: int = Field(25, ge=1)

    # Analytics engine
    max_sub_questions: int = Field(5, ge=1, le=10)
    max_parallel_workers: int = Field(1, ge=1, le=8)
    enable_sql_repair: bool = True
    enable_response_validator: bool = True
    insight_llm_narrative: bool = True

    # Persistent question-answer cache
    qa_cache_enabled: bool = True
    qa_cache_path: Path = PROJECT_ROOT / "data" / "question_answer.json"
    qa_cache_max_entries: int = Field(500, ge=1)
    qa_cache_max_entry_kb: int = Field(768, ge=16)

    # Outlook drafts
    insight_mail_client: Literal["outlook_web", "desktop"] = "outlook_web"
    insight_mail_cc: str = ""

    log_level: str = "INFO"

    @field_validator("qa_cache_path")
    @classmethod
    def _anchor_relative_path(cls, v: Path) -> Path:
        """Relative paths are resolved against the project root, not the working directory."""
        return v if v.is_absolute() else PROJECT_ROOT / v

    # ── derived helpers ────────────────────────────────────────────────────────
    @property
    def fq_table(self) -> str:
        """Back-ticked, fully qualified name of the approved table."""
        parts = [p for p in (self.databricks_catalog, self.databricks_schema) if p]
        if self.databricks_catalog and not self.databricks_schema:
            parts = []  # a catalog without a schema is not addressable
        return ".".join(f"`{p}`" for p in [*parts, self.databricks_table])

    @property
    def databricks_configured(self) -> bool:
        return bool(self.databricks_server_hostname and self.databricks_http_path
                    and self.databricks_access_token.get_secret_value())

    @property
    def llm_configured(self) -> bool:
        return bool(self.groq_api_key.get_secret_value())

    def ai_fingerprint(self, prompt_version: str, composer_version: str) -> str:
        """Hash of every setting that can change an AI answer (used for cache invalidation)."""
        material = {
            "model": self.groq_model, "temperature": self.llm_temperature, "seed": self.llm_seed,
            "response_format": self.llm_response_format, "table": self.fq_table,
            "max_sub_questions": self.max_sub_questions, "repair": self.enable_sql_repair,
            "validator": self.enable_response_validator, "narrative": self.insight_llm_narrative,
            "row_limit": self.max_row_query_limit, "result_rows": self.max_result_rows,
            "sla": SLA_DAYS, "escalation": ESCALATION_LEVEL, "aging": AGING_THRESHOLD,
            "prompt_version": prompt_version, "composer_version": composer_version,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings instance (restart the app after editing ``.env``)."""
    return Settings()
