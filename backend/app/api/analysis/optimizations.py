"""Analysis API router: project-wide & single-script optimization triggers and findings management."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.common import get_db
from . import optimizations_service

router = APIRouter(tags=["analysis"])


class ProjectAnalyzeRequest(BaseModel):
    run_llm: bool = False
    max_chain_hops: int = Field(default=3, ge=2, le=20)


class ScriptAnalyzeRequest(BaseModel):
    run_llm: bool = False
    max_chunks_to_refine: int = Field(default=3, ge=1, le=10)
    max_llm_calls: int = Field(default=8, ge=1, le=30)


class FindingStatusPatch(BaseModel):
    status: str
    resolution_note: Optional[str] = None


@router.get("/analysis/rules")
def read_rules():
    return optimizations_service.get_rule_catalog()


@router.post("/projects/{project_id}/analyze")
def trigger_project_analysis(
    project_id: str,
    payload: ProjectAnalyzeRequest = ProjectAnalyzeRequest(),
    db: Session = Depends(get_db),
):
    try:
        return optimizations_service.run_project_analysis(
            db,
            project_id,
            run_llm=payload.run_llm,
            max_chain_hops=payload.max_chain_hops,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/projects/{project_id}/scripts/{script_id}/analyze")
def trigger_script_analysis(
    project_id: str,
    script_id: str,
    payload: ScriptAnalyzeRequest = ScriptAnalyzeRequest(),
    db: Session = Depends(get_db),
):
    try:
        return optimizations_service.run_single_script_analysis(
            db,
            project_id,
            script_id,
            run_llm=payload.run_llm,
            max_chunks_to_refine=payload.max_chunks_to_refine,
            max_llm_calls=payload.max_llm_calls,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/projects/{project_id}/findings")
def list_project_findings(
    project_id: str,
    scope: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    script_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    items = optimizations_service.query_findings(
        db,
        project_id,
        scope=scope,
        severity=severity,
        script_id=script_id,
        status=status,
    )
    return {"findings": items, "total": len(items)}


@router.patch("/findings/{finding_id}")
def patch_finding_status(
    finding_id: str,
    payload: FindingStatusPatch,
    db: Session = Depends(get_db),
):
    try:
        return optimizations_service.mutate_finding_status(
            db,
            finding_id,
            status=payload.status,
            resolution_note=payload.resolution_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
