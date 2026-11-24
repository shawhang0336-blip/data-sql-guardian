"""Analysis domain service adapting Layer 5 optimization workflows for HTTP routers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.analysis import (
    SINGLE_SCRIPT_RULE_CATALOG,
    analyze_project_wide,
    analyze_single_script,
    list_findings,
    update_finding_status,
)


def run_project_analysis(
    db: Session,
    project_id: str,
    *,
    run_llm: bool = False,
    max_chain_hops: int = 3,
) -> Dict[str, Any]:
    return analyze_project_wide(
        db,
        project_id,
        run_llm=run_llm,
        max_chain_hops=max_chain_hops,
    )


def run_single_script_analysis(
    db: Session,
    project_id: str,
    script_id: str,
    *,
    run_llm: bool = False,
    max_chunks_to_refine: int = 3,
    max_llm_calls: int = 8,
) -> Dict[str, Any]:
    return analyze_single_script(
        db,
        project_id,
        script_id,
        run_llm=run_llm,
        max_chunks_to_refine=max_chunks_to_refine,
        max_llm_calls=max_llm_calls,
    )


def query_findings(
    db: Session,
    project_id: str,
    *,
    scope: Optional[str] = None,
    severity: Optional[str] = None,
    script_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return list_findings(
        db,
        project_id,
        scope=scope,
        severity=severity,
        script_id=script_id,
        status=status,
    )


def mutate_finding_status(
    db: Session,
    finding_id: str,
    *,
    status: str,
    resolution_note: Optional[str] = None,
) -> Dict[str, Any]:
    return update_finding_status(
        db,
        finding_id,
        status=status,
        resolution_note=resolution_note,
    )


def get_rule_catalog() -> Dict[str, Any]:
    return {"single_script_rules": SINGLE_SCRIPT_RULE_CATALOG}
