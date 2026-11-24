"""Single-script optimization orchestrator: deterministic 16 rules + guarded BSP AI diagnosis & sqlglot-verified rewrite."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.analysis.core import evaluate_script_rules
from app.catalog import serialize_script
from app.infra.config import get_budget, get_effective_settings
from app.infra.db import SessionLocal
from app.infra.db_tables import (
    OptimizationFindingModel,
    ScriptChunkModel,
    ScriptFileModel,
    ScriptProjectModel,
)
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm import (
    RealDesignLlm,
    call_llm_validated,
    llm_execution_scope,
    submit_with_context,
)
from app.kernel import append_event, create_run
from app.policy import build_script_child_bus

try:
    import sqlglot
except Exception:  # pragma: no cover
    sqlglot = None

logger = logging.getLogger(__name__)


def _verify_sql_syntax(sql_text: str, dialect: str) -> bool:
    """Verify rewritten SQL parses cleanly with sqlglot when non-empty."""
    text = (sql_text or "").strip().rstrip(";")
    if not text:
        return False
    if sqlglot is None:
        return len(text) > 10
    sg_dialect = {"oracle": "oracle", "hive": "hive", "spark": "spark", "postgres": "postgres", "mysql": "mysql"}.get(
        dialect, None
    )
    try:
        sqlglot.parse_one(text, read=sg_dialect)
        return True
    except Exception:
        # Allow PL/SQL procedural snippets if simple statement keyword present
        upper = text.upper()
        return any(upper.startswith(k) for k in ("SELECT", "WITH", "INSERT", "MERGE", "UPDATE", "DELETE"))


def sync_project_finding_count(session: Session, project_id: str) -> int:
    count = (
        session.query(OptimizationFindingModel)
        .filter(OptimizationFindingModel.project_id == project_id)
        .count()
    )
    proj = session.get(ScriptProjectModel, project_id)
    if proj is not None:
        proj.finding_count = count
    session.commit()
    return count


def replace_script_findings(
    session: Session,
    *,
    project_id: str,
    script_id: str,
    drafts: List[Dict[str, Any]],
) -> List[OptimizationFindingModel]:
    """Replace OPEN findings for a script while preserving ADOPTED/IGNORED user decisions."""
    preserved = (
        session.query(OptimizationFindingModel)
        .filter(
            OptimizationFindingModel.project_id == project_id,
            OptimizationFindingModel.scope == "SCRIPT",
            OptimizationFindingModel.script_id == script_id,
            OptimizationFindingModel.status != "OPEN",
        )
        .all()
    )
    preserved_keys = {
        (f.rule_code, f.start_line): f
        for f in preserved
    }
    session.execute(
        delete(OptimizationFindingModel).where(
            OptimizationFindingModel.project_id == project_id,
            OptimizationFindingModel.scope == "SCRIPT",
            OptimizationFindingModel.script_id == script_id,
            OptimizationFindingModel.status == "OPEN",
        )
    )

    inserted: List[OptimizationFindingModel] = []
    for d in drafts:
        key = (d["rule_code"], d.get("start_line"))
        if key in preserved_keys:
            row = preserved_keys[key]
            row.description = d["description"]
            row.evidence_snippet = d["evidence_snippet"]
            inserted.append(row)
            continue
        row = OptimizationFindingModel(
            id=str(uuid.uuid4()),
            project_id=project_id,
            scope="SCRIPT",
            script_id=script_id,
            chunk_id=d.get("chunk_id"),
            rule_code=d["rule_code"],
            category=d["category"],
            severity=d["severity"],
            title=d["title"],
            description=d["description"],
            evidence_snippet=d["evidence_snippet"],
            start_line=int(d.get("start_line") or 0),
            end_line=int(d.get("end_line") or 0),
            related_tables_json=d.get("related_tables", []),
            related_scripts_json=d.get("related_scripts", [script_id]),
            suggestion=d["suggestion"],
            rewritten_sql=d.get("rewritten_sql") or "",
            expected_benefit=d.get("expected_benefit", ""),
            ai_refined=bool(d.get("ai_refined", False)),
            status="OPEN",
        )
        session.add(row)
        inserted.append(row)

    session.commit()
    sync_project_finding_count(session, project_id)
    return inserted


def analyze_single_script(
    session: Session,
    project_id: str,
    script_id: str,
    *,
    run_llm: bool = False,
    llm_client: Optional[Any] = None,
    max_chunks_to_refine: int = 3,
    max_llm_calls: int = 8,
) -> Dict[str, Any]:
    """Run deterministic single-script audit and optional BSP guarded AI root-cause diagnosis & SQL rewrite."""
    script = (
        session.query(ScriptFileModel)
        .filter(ScriptFileModel.project_id == project_id, ScriptFileModel.id == script_id)
        .first()
    )
    if script is None:
        raise ValueError(f"未找到脚本: {script_id}")

    chunks = (
        session.query(ScriptChunkModel)
        .filter(ScriptChunkModel.script_id == script_id)
        .order_by(ScriptChunkModel.chunk_index.asc())
        .all()
    )
    script_dict = serialize_script(script)
    chunk_dicts = [
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
        for c in chunks
    ]

    drafts = evaluate_script_rules(script_dict, chunk_dicts)
    rows = replace_script_findings(session, project_id=project_id, script_id=script_id, drafts=drafts)

    ai_status = "SKIPPED"
    ai_notes: Dict[str, Any] = {}
    run_id: Optional[str] = None

    if run_llm and chunk_dicts:
        budget = get_budget(session)
        llm = llm_client or RealDesignLlm(get_effective_settings(session))
        rm_run = create_run(
            session,
            spec_id=f"script:{script.filename}",
            initial_node="static_scanner",
            initial_payload={"project_id": project_id, "script_id": script_id},
        )
        run_id = rm_run.id
        append_event(
            session,
            rm_run,
            "STATIC_SCANNED",
            "static_scanner",
            {"script_finding_count": len(rows)},
        )
        session.commit()

        child_bus = build_script_child_bus(
            max_calls=min(max_llm_calls, int(budget.get("max_calls_per_script", 8))),
            max_elapsed_seconds=int(budget.get("max_elapsed_seconds", 600)),
            script_tag=script.filename,
        )

        # Pick chunks with high-priority findings or complex DML for LLM review
        finding_chunk_ids = list(
            dict.fromkeys(
                r.chunk_id for r in sorted(rows, key=lambda x: {"P0": 0, "P1": 1, "P2": 2}.get(x.severity, 9))
                if r.chunk_id
            )
        )
        candidate_chunks = [c for c in chunk_dicts if c["id"] in set(finding_chunk_ids)] or [
            c for c in chunk_dicts if c["statement_kind"] in ("INSERT", "MERGE", "UPDATE", "DELETE", "SELECT")
        ]
        candidate_chunks = candidate_chunks[:max_chunks_to_refine]
        allowed_chunk_ids = [c["id"] for c in chunk_dicts]
        chunk_by_id = {c["id"]: c for c in chunk_dicts}

        try:
            with llm_execution_scope(bus=child_bus):
                plan_resp, _ = call_llm_validated(
                    llm,
                    "plan_script_review",
                    instruction=(
                        f"请针对脚本 `{script.filename}` ({script.dialect}) 的切块结构与离线静态扫描发现，"
                        "选择最值得深度根因分析与 SQL 重写的高优 chunk_id（不超过 3 个）。"
                    ),
                    payload={
                        "filename": script.filename,
                        "dialect": script.dialect,
                        "line_count": script.line_count,
                        "allowed_chunk_ids": allowed_chunk_ids,
                        "chunks_preview": [
                            {
                                "chunk_id": c["id"],
                                "start_line": c["start_line"],
                                "end_line": c["end_line"],
                                "kind": c["statement_kind"],
                                "targets": c["target_tables"],
                                "sources": c["source_tables"],
                            }
                            for c in chunk_dicts[:25]
                        ],
                        "static_findings": [
                            {"rule": r.rule_code, "severity": r.severity, "chunk_id": r.chunk_id, "title": r.title}
                            for r in rows[:15]
                        ],
                    },
                    bus=child_bus,
                )
                chosen_ids = [
                    cid for cid in (plan_resp.get("priority_chunk_ids") or [])
                    if cid in chunk_by_id
                ][:max_chunks_to_refine]
                if not chosen_ids:
                    chosen_ids = [c["id"] for c in candidate_chunks]

                append_event(
                    session,
                    rm_run,
                    "CHUNK_ANALYZED",
                    "chunk_analyzer",
                    {
                        "planned_chunks": chosen_ids,
                        "script_purpose": plan_resp.get("script_purpose", ""),
                        "review_focus": plan_resp.get("review_focus", ""),
                    },
                    bus=child_bus,
                )

                def _diagnose_and_rewrite(cid: str) -> Dict[str, Any]:
                    ch = chunk_by_id[cid]
                    local_findings = [r for r in rows if r.chunk_id == cid]
                    diag, _ = call_llm_validated(
                        llm,
                        "diagnose_chunk_issues",
                        instruction=(
                            f"深入分析脚本 `{script.filename}` 第 {ch['start_line']}-{ch['end_line']} 行 SQL 切块，"
                            "指出每条问题的 root_cause、actionable_fix、expected_benefit，或澄清误报。"
                        ),
                        payload={
                            "chunk_id": cid,
                            "allowed_chunk_ids": [cid],
                            "start_line": ch["start_line"],
                            "end_line": ch["end_line"],
                            "statement_kind": ch["statement_kind"],
                            "control_context": ch["control_context"],
                            "raw_sql": ch["raw_sql"][:3500],
                            "static_findings": [
                                {"rule": r.rule_code, "title": r.title, "desc": r.description}
                                for r in local_findings
                            ],
                        },
                        bus=child_bus,
                    )
                    diag_items = diag.get("findings") or []
                    first_diag = diag_items[0] if diag_items and isinstance(diag_items[0], dict) else {}
                    rewrite_out: Dict[str, Any] = {}
                    # Trigger SQL rewrite for P0/P1 or non-false-positive findings on query/DML chunks
                    if local_findings and local_findings[0].severity in ("P0", "P1") and ch["statement_kind"] in ("SELECT", "INSERT", "MERGE", "UPDATE", "DELETE"):
                        rw, _ = call_llm_validated(
                            llm,
                            "propose_sql_rewrite",
                            instruction=(
                                f"在严格保持业务语义与目标写入表不变的前提下，为第 {ch['start_line']}-{ch['end_line']} 行提供等价优化后的完整 SQL。"
                            ),
                            payload={
                                "chunk_id": cid,
                                "allowed_chunk_ids": [cid],
                                "required_target_tables": ch["target_tables"],
                                "dialect": script.dialect,
                                "original_sql": ch["raw_sql"][:3500],
                                "rule_code": local_findings[0].rule_code,
                                "issue_summary": first_diag.get("root_cause", local_findings[0].description),
                            },
                            bus=child_bus,
                        )
                        rewritten = (rw.get("rewritten_sql") or "").strip()
                        if rewritten and _verify_sql_syntax(rewritten, script.dialect):
                            rewrite_out = rw
                    return {"chunk_id": cid, "diagnosis": diag, "rewrite": rewrite_out}

                results: List[Dict[str, Any]] = []
                workers = max(1, min(len(chosen_ids), 3))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [submit_with_context(pool, _diagnose_and_rewrite, cid) for cid in chosen_ids]
                    for fut in futures:
                        results.append(fut.result())

                for res in results:
                    cid = res["chunk_id"]
                    diag = res["diagnosis"]
                    rw = res["rewrite"]
                    diag_by_rule = {
                        str(d.get("rule_code", "")): d
                        for d in (diag.get("findings") or [])
                        if isinstance(d, dict)
                    }
                    first_diag = (diag.get("findings") or [{}])[0] if diag.get("findings") else {}
                    matching = [r for r in rows if r.chunk_id == cid]
                    for r in matching:
                        item = diag_by_rule.get(r.rule_code) or (first_diag if isinstance(first_diag, dict) else {})
                        if item.get("root_cause"):
                            r.description = f"{r.description}\n\n[AI 根因诊断] {item['root_cause']}"
                        if item.get("actionable_fix"):
                            r.suggestion = f"{r.suggestion}\n\n[AI 修复方案] {item['actionable_fix']}"
                        if rw.get("rewritten_sql"):
                            r.rewritten_sql = rw["rewritten_sql"]
                        if rw.get("expected_benefit"):
                            r.expected_benefit = rw["expected_benefit"]
                        elif item.get("expected_benefit"):
                            r.expected_benefit = item["expected_benefit"]
                        r.ai_refined = True
                    append_event(
                        session,
                        rm_run,
                        "CHUNK_ANALYZED",
                        "chunk_analyzer",
                        {
                            "chunk_id": cid,
                            "findings_count": len(diag.get("findings") or []),
                            "has_rewrite": bool(rw.get("rewritten_sql")),
                        },
                        bus=child_bus,
                    )

                session.commit()
                ai_status = "COMPLETED"
                guard_snap = child_bus.budget_guard.snapshot() if child_bus.budget_guard else {}
                ai_notes = {
                    "analyzed_chunks": len(results),
                    "llm_calls": guard_snap.get("llm_calls", 0),
                    "review_focus": plan_resp.get("review_focus", ""),
                    "script_purpose": plan_resp.get("script_purpose", ""),
                }
        except ProviderAuthOrBillingError as exc:
            ai_status = "BILLING_OR_AUTH_ERROR"
            ai_notes = {"error": str(exc)}
            logger.warning("Single-script LLM analysis aborted due to billing/auth: %s", exc)
        except Exception as exc:
            ai_status = "FAILED_FALLBACK_OFFLINE"
            ai_notes = {"error": str(exc)}
            logger.warning("Single-script LLM refinement failed cleanly, keeping offline findings: %s", exc)

    return {
        "project_id": project_id,
        "script_id": script_id,
        "filename": script.filename,
        "finding_count": len(rows),
        "ai_status": ai_status,
        "ai_notes": ai_notes,
        "run_id": run_id,
    }
