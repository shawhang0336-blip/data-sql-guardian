"""Platform health and global dashboard endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.common import get_db
from app.infra.config import get_settings
from app.infra.db_tables import (
    ChangeReviewModel,
    OptimizationFindingModel,
    ScriptChunkModel,
    ScriptFileModel,
    ScriptProjectModel,
    TableSchemaModel,
)

router = APIRouter(prefix="/system", tags=["platform"])


@router.get("/health")
def health_check():
    cfg = get_settings()
    return {"status": "ok", "app_name": cfg.app_name}


@router.get("/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    return {
        "project_count": db.query(ScriptProjectModel).count(),
        "script_count": db.query(ScriptFileModel).count(),
        "chunk_count": db.query(ScriptChunkModel).count(),
        "table_count": db.query(TableSchemaModel).count(),
        "open_finding_count": db.query(OptimizationFindingModel).filter(OptimizationFindingModel.status == "OPEN").count(),
        "p0_finding_count": db.query(OptimizationFindingModel).filter(OptimizationFindingModel.severity == "P0").count(),
        "pending_review_count": db.query(ChangeReviewModel).filter(ChangeReviewModel.status == "WAITING_APPROVAL").count(),
        "total_review_count": db.query(ChangeReviewModel).count(),
    }
