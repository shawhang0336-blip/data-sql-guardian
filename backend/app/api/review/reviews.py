"""Review API router: pre-release ADD/MODIFY script evaluation, release gate approval/rejection, Markdown export, trace."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.common import get_db
from app.infra.config import OPERATOR_PLACEHOLDER
from . import reviews_service

router = APIRouter(tags=["review"])


class CreateChangeReviewRequest(BaseModel):
    change_type: str  # ADD | MODIFY
    filename: str
    new_content: str
    target_script_id: Optional[str] = None
    relative_path: str = ""
    author: str = OPERATOR_PLACEHOLDER
    change_summary: str = ""
    run_llm: bool = False


class ApprovalDecisionRequest(BaseModel):
    reviewer: str = OPERATOR_PLACEHOLDER
    comment: str = ""
    merge_baseline: bool = True


@router.post("/projects/{project_id}/reviews")
def create_review(
    project_id: str,
    payload: CreateChangeReviewRequest,
    db: Session = Depends(get_db),
):
    try:
        return reviews_service.evaluate_script_change(
            db,
            project_id,
            change_type=payload.change_type,
            filename=payload.filename,
            new_content=payload.new_content,
            target_script_id=payload.target_script_id,
            relative_path=payload.relative_path,
            author=payload.author,
            change_summary=payload.change_summary,
            run_llm=payload.run_llm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/reviews")
def list_reviews(project_id: str, db: Session = Depends(get_db)):
    return {"reviews": reviews_service.list_change_reviews(db, project_id)}


@router.get("/reviews/{review_id}")
def get_review(review_id: str, db: Session = Depends(get_db)):
    detail = reviews_service.get_change_review_detail(db, review_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"变更评估单不存在: {review_id}")
    return detail


@router.post("/reviews/{review_id}/approve")
def approve_review(
    review_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    db: Session = Depends(get_db),
):
    try:
        return reviews_service.approve_change_review(
            db,
            review_id,
            reviewer=payload.reviewer,
            comment=payload.comment,
            merge_baseline=payload.merge_baseline,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reviews/{review_id}/reject")
def reject_review(
    review_id: str,
    payload: ApprovalDecisionRequest = ApprovalDecisionRequest(),
    db: Session = Depends(get_db),
):
    try:
        return reviews_service.reject_change_review(
            db,
            review_id,
            reviewer=payload.reviewer,
            comment=payload.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reviews/{review_id}/export")
def export_review(
    review_id: str,
    format: str = Query(default="json"),
    db: Session = Depends(get_db),
):
    try:
        md = reviews_service.export_review_markdown(db, review_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if format.lower() in ("md", "markdown"):
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")
    return {"review_id": review_id, "markdown": md}


@router.get("/runs/{run_id}/trace")
def read_run_trace(run_id: str, db: Session = Depends(get_db)):
    trace = reviews_service.get_review_run_trace(db, run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Run 不存在: {run_id}")
    return trace
