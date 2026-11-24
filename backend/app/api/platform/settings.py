"""Platform LLM configuration, execution budget, and live connection test endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.common import get_db
from app.infra.config import (
    DEFAULT_BUDGET,
    get_budget,
    get_effective_settings,
    get_llm_config,
)
from app.infra.db_tables import SystemSettingModel
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm import (
    RealDesignLlm,
    configure_llm_concurrency,
    configure_llm_rate_limit,
)

router = APIRouter(prefix="/settings", tags=["platform"])


class LlmSettingsUpdate(BaseModel):
    llm_provider: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_timeout: Optional[int] = Field(default=None, ge=5, le=600)


class BudgetSettingsUpdate(BaseModel):
    max_retries: Optional[int] = Field(default=None, ge=0, le=10)
    max_llm_concurrency: Optional[int] = Field(default=None, ge=1, le=32)
    max_llm_rpm: Optional[int] = Field(default=None, ge=1, le=1000)
    max_calls_per_script: Optional[int] = Field(default=None, ge=1, le=100)
    max_llm_calls: Optional[int] = Field(default=None, ge=1, le=5000)
    max_total_tokens: Optional[int] = Field(default=None, ge=1000, le=50000000)
    max_elapsed_seconds: Optional[int] = Field(default=None, ge=30, le=7200)


@router.get("")
def read_settings(db: Session = Depends(get_db)):
    return {
        "llm": get_llm_config(db),
        "budget": get_budget(db),
        "default_budget": DEFAULT_BUDGET,
    }


@router.put("/llm")
def update_llm_settings(payload: LlmSettingsUpdate, db: Session = Depends(get_db)):
    row = db.get(SystemSettingModel, "llm_config")
    existing = dict(row.value_json) if (row and isinstance(row.value_json, dict)) else {}
    for key in ("llm_provider", "llm_base_url", "llm_model", "llm_timeout"):
        val = getattr(payload, key)
        if val is not None:
            existing[key] = val
    if payload.llm_api_key is not None and payload.llm_api_key.strip():
        existing["llm_api_key"] = payload.llm_api_key.strip()

    if row is None:
        row = SystemSettingModel(key="llm_config", value_json=existing)
        db.add(row)
    else:
        row.value_json = existing
    db.commit()
    return {"llm": get_llm_config(db)}


@router.put("/budget")
def update_budget_settings(payload: BudgetSettingsUpdate, db: Session = Depends(get_db)):
    row = db.get(SystemSettingModel, "execution_budget")
    existing = dict(row.value_json) if (row and isinstance(row.value_json, dict)) else {}
    for k in DEFAULT_BUDGET.keys():
        val = getattr(payload, k, None)
        if val is not None:
            existing[k] = val
    if row is None:
        row = SystemSettingModel(key="execution_budget", value_json=existing)
        db.add(row)
    else:
        row.value_json = existing
    db.commit()

    merged = get_budget(db)
    configure_llm_concurrency(int(merged.get("max_llm_concurrency", 4)))
    configure_llm_rate_limit(int(merged.get("max_llm_rpm", 60)))
    return {"budget": merged}


@router.post("/llm/test")
def test_llm_connection(db: Session = Depends(get_db)):
    eff = get_effective_settings(db)
    if eff.llm_provider == "scripted":
        return {"ok": True, "provider": eff.llm_provider, "message": "当前为离线 Scripted 模式，无需外部 API。"}
    if not eff.llm_api_key:
        return {"ok": False, "status": "MISSING_KEY", "message": "未配置 LLM_API_KEY"}
    try:
        client = RealDesignLlm(eff)
        res, _trace = client.assign(
            "summarize_release_gate",
            "连通性自检，返回 PASS",
            {"check": "connectivity", "deterministic_verdict": "PASS"},
        )
        return {"ok": True, "provider": eff.llm_provider, "model": eff.llm_model, "sample_response": res}
    except ProviderAuthOrBillingError as exc:
        return {"ok": False, "status": "BILLING_OR_AUTH_ERROR", "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "status": "ERROR", "message": str(exc)}
