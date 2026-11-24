"""运行配置、两级预算存储与模型配置读取（基础设施层 Layer 0）。"""

from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).resolve().parents[3]
OPERATOR_PLACEHOLDER = "评审工程师"

DEFAULT_BUDGET: Dict[str, int] = {
    "max_retries": 3,
    "max_llm_concurrency": 4,
    "max_llm_rpm": 60,
    "max_calls_per_script": 8,
    "max_llm_calls": 600,
    "max_total_tokens": 10000000,
    "max_elapsed_seconds": 3600,
}


class Settings(BaseSettings):
    app_name: str = "Data SQL Guardian — SQL 治理优化与发布门禁台"
    database_url: str = f"sqlite:///{ROOT_DIR / 'backend' / 'var' / 'data_sql_guardian.db'}"
    blob_dir: str = str(ROOT_DIR / "backend" / "var" / "blobs")
    sql_explorer_dir: Optional[str] = None
    llm_provider: str = "scripted"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_api_key: Optional[str] = None
    llm_thinking: Optional[str] = None
    llm_timeout: int = 120
    llm_tool_choice: str = "auto"
    cors_origins: str = "http://localhost:5175,http://127.0.0.1:5175,http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / ".env"), extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_budget(session: Session) -> Dict[str, int]:
    """读取执行预算配置，缺失项以 DEFAULT_BUDGET 兜底。"""
    from app.infra.db_tables import SystemSettingModel

    row = session.get(SystemSettingModel, "execution_budget")
    stored = row.value_json if (row is not None and isinstance(row.value_json, dict)) else {}
    return {**DEFAULT_BUDGET, **{k: v for k, v in stored.items() if v is not None}}


def get_llm_config(session: Session, settings: Optional[Settings] = None) -> Dict[str, object]:
    """读取持久化 LLM 配置（脱敏，绝不回传明文 API Key）。"""
    from app.infra.db_tables import SystemSettingModel

    base = settings or get_settings()
    row = session.get(SystemSettingModel, "llm_config")
    stored = row.value_json if (row is not None and isinstance(row.value_json, dict)) else {}
    api_key = str(stored.get("llm_api_key") or base.llm_api_key or "")
    return {
        "llm_provider": str(stored.get("llm_provider") or base.llm_provider),
        "llm_base_url": str(stored.get("llm_base_url") or base.llm_base_url),
        "llm_model": str(stored.get("llm_model") or base.llm_model),
        "llm_timeout": int(stored.get("llm_timeout") or base.llm_timeout),
        "has_api_key": bool(api_key),
        "llm_api_key": "",
    }


def get_effective_settings(session: Session, settings: Optional[Settings] = None) -> Settings:
    """结合环境变量与数据库配置生成本次运行的 Settings 快照。"""
    from app.infra.db_tables import SystemSettingModel

    base = settings or get_settings()
    row = session.get(SystemSettingModel, "llm_config")
    stored = row.value_json if (row is not None and isinstance(row.value_json, dict)) else {}
    if not stored:
        return base
    updates: Dict[str, object] = {}
    for field in ("llm_provider", "llm_base_url", "llm_model", "llm_timeout"):
        if stored.get(field) is not None and stored.get(field) != "":
            updates[field] = stored[field]
    if stored.get("llm_api_key"):
        updates["llm_api_key"] = stored["llm_api_key"]
    return base.model_copy(update=updates)
