"""Project-wide analysis orchestrator: full offline rule sweep + guarded AI refactoring roadmap."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.analysis.core import evaluate_project_rules, evaluate_script_rules
from app.catalog import rebuild_project_lineage, serialize_script
from app.infra.config import get_budget, get_effective_settings
from app.infra.db_tables import (
    OptimizationFindingModel,
    ScriptChunkModel,
    ScriptFileModel,
    ScriptProjectModel,
)
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm import RealDesignLlm, call_llm_validated, llm_execution_scope
from app.kernel import append_event, create_run
from app.policy import build_run_bus
from .single_script_analyzer import replace_script_findings, sync_project_finding_count

logger = logging.getLogger(__name__)


def _replace_project_scope_findings(
    session: Session,
    *,
    project_id: str,
    drafts: List[Dict[str, Any]],
) -> List[OptimizationFindingModel]:
    preserved = (
        session.query(OptimizationFindingModel)
        .filter(
            OptimizationFindingModel.project_id == project_id,
            OptimizationFindingModel.scope == "PROJECT",
            OptimizationFindingModel.status != "OPEN",
        )
        .all()
    )
    preserved_keys = {(f.rule_code, f.evidence_snippet): f for f in preserved}

    session.execute(
        delete(OptimizationFindingModel).where(
            OptimizationFindingModel.project_id == project_id,
            OptimizationFindingModel.scope == "PROJECT",
            OptimizationFindingModel.status == "OPEN",
        )
    )

    rows: List[OptimizationFindingModel] = []
    for d in drafts:
        key = (d["rule_code"], d["evidence_snippet"])
        if key in preserved_keys:
            rows.append(preserved_keys[key])
            continue
        row = OptimizationFindingModel(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scope="PROJECT",
            script_id=None,
            chunk_id=None,
            rule_code=d["rule_code"],
            category=d["category"],
            severity=d["severity"],
            title=d["title"],
            description=d["description"],
            evidence_snippet=d["evidence_snippet"],
            start_line=0,
            end_line=0,
            related_tables_json=d.get("related_tables", []),
            related_scripts_json=d.get("related_scripts", []),
            suggestion=d["suggestion"],
            rewritten_sql="",
            expected_benefit=d.get("expected_benefit", ""),
            ai_refined=bool(d.get("ai_refined", False)),
            status="OPEN",
        )
        session.add(row)
        rows.append(row)

    session.commit()
    return rows


def analyze_project_wide(
    session: Session,
    project_id: str,
    *,
    run_llm: bool = False,
    llm_client: Optional[Any] = None,
    max_chain_hops: int = 3,
) -> Dict[str, Any]:
    """Run all 16 single-script rules + 6 project-wide rules on the entire repository, plus optional AI roadmap."""
    project = session.get(ScriptProjectModel, project_id)
    if project is None:
        raise ValueError(f"未找到项目: {project_id}")

    run = create_run(
        session,
        spec_id=f"project:{project.name}",
        initial_node="static_scanner",
        initial_payload={"project_id": project_id, "run_llm": run_llm},
    )

    topology = rebuild_project_lineage(session, project_id)

    scripts = (
        session.query(ScriptFileModel)
        .filter(ScriptFileModel.project_id == project_id)
        .order_by(ScriptFileModel.filename.asc())
        .all()
    )
    all_chunks = (
        session.query(ScriptChunkModel)
        .filter(ScriptChunkModel.project_id == project_id)
        .order_by(ScriptChunkModel.script_id.asc(), ScriptChunkModel.chunk_index.asc())
        .all()
    )
    chunks_by_script: Dict[str, List[Dict[str, Any]]] = {}
    for c in all_chunks:
        chunks_by_script.setdefault(c.script_id, []).append(
            {
                "id": c.id,
                "chunk_index": c.chunk_index,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "statement_kind": c.statement_kind,
                "is_dynamic_sql": c.is_dynamic_sql,
                "control_context": c.control_context,
                "target_tables": c.target_tables_json or [],
                "source_tables": c.source_tables_json or [],
                "ast_features": c.ast_features_json or {},
                "ast_fingerprint": c.ast_fingerprint,
                "raw_sql": c.raw_sql,
            }
        )

    script_finding_total = 0
    for s in scripts:
        s_dict = serialize_script(s)
        s_chunks = chunks_by_script.get(s.id, [])
        drafts = evaluate_script_rules(s_dict, s_chunks)
        rows = replace_script_findings(session, project_id=project_id, script_id=s.id, drafts=drafts)
        script_finding_total += len(rows)

    project_drafts = evaluate_project_rules(topology, max_chain_hops=max_chain_hops)
    project_rows = _replace_project_scope_findings(session, project_id=project_id, drafts=project_drafts)
    total_findings = sync_project_finding_count(session, project_id)

    append_event(
        session,
        run,
        "STATIC_SCANNED",
        "static_scanner",
        {
            "script_findings": script_finding_total,
            "project_findings": len(project_rows),
            "total_findings": total_findings,
        },
    )
    session.commit()

    ai_status = "SKIPPED"
    ai_roadmap: Dict[str, Any] = {}

    if run_llm:
        budget = get_budget(session)
        llm = llm_client or RealDesignLlm(get_effective_settings(session))
        run_bus = build_run_bus(budget=budget, run_id=run.id)

        known_tables = sorted(
            {
                t
                for n in topology.get("nodes", [])
                for t in (n.get("target_tables", []) + n.get("source_tables", []))
            }
            | set(topology.get("dead_or_orphan_tables", []))
            | set(topology.get("missing_ddl_tables", []))
        )

        try:
            with llm_execution_scope(bus=run_bus):
                roadmap, _ = call_llm_validated(
                    llm,
                    "synthesize_project_optimizations",
                    instruction=(
                        f"请立足 `{project.name}` 全项目视角，结合血缘拓扑、共享表冲突、长链路、核心枢纽表及脚本静态问题，"
                        "输出分阶段系统性架构治理路线图与可复用公共层重构方案。所有提及表名必须严格属于 allowed_tables。"
                    ),
                    payload={
                        "project_name": project.name,
                        "script_count": len(scripts),
                        "chunk_count": len(all_chunks),
                        "allowed_tables": known_tables,
                        "topology_summary": {
                            "cycles": topology.get("cycles", []),
                            "longest_chain": topology.get("longest_chain", []),
                            "write_conflict_tables": topology.get("write_conflict_tables", []),
                            "truncate_conflicts": topology.get("truncate_conflicts", []),
                            "hub_tables": topology.get("hub_tables", [])[:8],
                            "duplicate_clusters_count": len(topology.get("duplicate_clusters", [])),
                        },
                        "project_findings": [
                            {
                                "rule": r.rule_code,
                                "severity": r.severity,
                                "title": r.title,
                                "tables": r.related_tables_json,
                                "description": r.description,
                            }
                            for r in project_rows[:12]
                        ],
                    },
                    bus=run_bus,
                )
            ai_roadmap = roadmap
            summary = dict(project.summary_json or {})
            summary["ai_roadmap"] = roadmap
            project.summary_json = summary
            for pr in project_rows:
                pr.ai_refined = True
            session.commit()
            append_event(
                session,
                run,
                "PROJECT_SYNTHESIZED",
                "project_synthesizer",
                {
                    "executive_summary": roadmap.get("executive_summary", ""),
                    "recommendations_count": len(roadmap.get("architecture_recommendations", [])),
                },
                bus=run_bus,
            )
            session.commit()
            ai_status = "COMPLETED"
        except ProviderAuthOrBillingError as exc:
            ai_status = "BILLING_OR_AUTH_ERROR"
            ai_roadmap = {"error": str(exc)}
            logger.warning("Project-wide AI synthesis aborted on auth/billing: %s", exc)
        except Exception as exc:
            ai_status = "FAILED_FALLBACK_OFFLINE"
            ai_roadmap = {"error": str(exc)}
            logger.warning("Project-wide AI synthesis failed cleanly: %s", exc)

    return {
        "project_id": project_id,
        "run_id": run.id,
        "script_findings": script_finding_total,
        "project_findings": len(project_rows),
        "total_findings": total_findings,
        "ai_status": ai_status,
        "ai_roadmap": ai_roadmap,
    }


def list_findings(
    session: Session,
    project_id: str,
    *,
    scope: Optional[str] = None,
    severity: Optional[str] = None,
    script_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    q = session.query(OptimizationFindingModel).filter(OptimizationFindingModel.project_id == project_id)
    if scope:
        q = q.filter(OptimizationFindingModel.scope == scope.upper())
    if severity:
        q = q.filter(OptimizationFindingModel.severity == severity.upper())
    if script_id:
        q = q.filter(OptimizationFindingModel.script_id == script_id)
    if status:
        q = q.filter(OptimizationFindingModel.status == status.upper())

    rows = q.order_by(OptimizationFindingModel.severity.asc(), OptimizationFindingModel.rule_code.asc()).all()
    script_names = {
        s.id: s.filename
        for s in session.query(ScriptFileModel).filter(ScriptFileModel.project_id == project_id).all()
    }
    return [
        {
            "id": r.id,
            "project_id": r.project_id,
            "scope": r.scope,
            "script_id": r.script_id,
            "script_filename": script_names.get(r.script_id, "") if r.script_id else "",
            "chunk_id": r.chunk_id,
            "rule_code": r.rule_code,
            "category": r.category,
            "severity": r.severity,
            "title": r.title,
            "description": r.description,
            "evidence_snippet": r.evidence_snippet,
            "start_line": r.start_line,
            "end_line": r.end_line,
            "related_tables": r.related_tables_json or [],
            "related_scripts": r.related_scripts_json or [],
            "suggestion": r.suggestion,
            "rewritten_sql": r.rewritten_sql,
            "expected_benefit": r.expected_benefit,
            "ai_refined": r.ai_refined,
            "status": r.status,
            "resolution_note": r.resolution_note,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def update_finding_status(
    session: Session,
    finding_id: str,
    *,
    status: str,
    resolution_note: Optional[str] = None,
) -> Dict[str, Any]:
    normalized = status.strip().upper()
    if normalized not in ("OPEN", "ADOPTED", "IGNORED"):
        raise ValueError(f"不支持的建议状态: {status}")
    row = session.get(OptimizationFindingModel, finding_id)
    if row is None:
        raise ValueError(f"未找到优化建议: {finding_id}")
    row.status = normalized
    if resolution_note is not None:
        row.resolution_note = resolution_note
    session.commit()
    session.refresh(row)
    return {
        "id": row.id,
        "status": row.status,
        "resolution_note": row.resolution_note,
    }
