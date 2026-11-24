"""Pre-release change evaluation engine: AST Diff, blast radius simulation, deterministic gate, guarded AI review, and Interrupt suspension."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.analysis.core import evaluate_script_rules
from app.catalog import (
    build_lineage_topology,
    compute_blast_radius,
    compute_script_ast_diff,
    load_script_raw_text,
)
from app.infra.blob_store import blob_store
from app.infra.config import OPERATOR_PLACEHOLDER, get_budget, get_effective_settings
from app.infra.db_tables import (
    ChangeReviewModel,
    ScriptChunkModel,
    ScriptFileModel,
    ScriptProjectModel,
    TableSchemaModel,
)
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm import RealDesignLlm, call_llm_validated, llm_execution_scope
from app.ingestion import aggregate_script_features, analyze_chunk_ast
from app.kernel import append_event, create_run, make_interrupt_payload
from app.policy import build_run_bus

logger = logging.getLogger(__name__)

_VERDICT_ORDER = {"PASS": 0, "WARN": 1, "BLOCK": 2}


def _higher_verdict(a: str, b: str) -> str:
    au = (a or "PASS").upper()
    bu = (b or "PASS").upper()
    return au if _VERDICT_ORDER.get(au, 0) >= _VERDICT_ORDER.get(bu, 0) else bu


def evaluate_script_change(
    session: Session,
    project_id: str,
    *,
    change_type: str,
    filename: str,
    new_content: str,
    target_script_id: Optional[str] = None,
    relative_path: str = "",
    author: str = OPERATOR_PLACEHOLDER,
    change_summary: str = "",
    run_llm: bool = False,
    llm_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Evaluate a proposed `ADD` or `MODIFY` script change and pause on human approval Interrupt."""
    project = session.get(ScriptProjectModel, project_id)
    if project is None:
        raise ValueError(f"未找到项目: {project_id}")

    ctype = change_type.strip().upper()
    if ctype not in ("ADD", "MODIFY"):
        raise ValueError(f"不支持的变更类型: {change_type}")
    if not (new_content or "").strip():
        raise ValueError("新脚本内容不能为空")

    target_script: Optional[ScriptFileModel] = None
    old_sql = ""
    old_blob_uri: str = ""

    if ctype == "MODIFY":
        if target_script_id:
            target_script = (
                session.query(ScriptFileModel)
                .filter(ScriptFileModel.project_id == project_id, ScriptFileModel.id == target_script_id)
                .first()
            )
        if target_script is None:
            target_script = (
                session.query(ScriptFileModel)
                .filter(ScriptFileModel.project_id == project_id, ScriptFileModel.filename == filename)
                .first()
            )
        if target_script is None:
            raise ValueError(f"MODIFY 变更未找到基线脚本 (script_id={target_script_id}, filename={filename})")
        filename = target_script.filename
        relative_path = relative_path or target_script.relative_path
        old_sql = load_script_raw_text(target_script)
        old_blob_uri = target_script.blob_uri
    else:
        relative_path = relative_path or filename

    dialect = (
        target_script.dialect
        if target_script is not None
        else (None if project.dialect in ("auto", "") else project.dialect)
    )
    review_id = str(uuid.uuid4())
    new_blob_uri = blob_store.put_text(
        new_content,
        subdir=f"reviews/{project_id}/{review_id}",
        filename=filename,
    )

    diff = compute_script_ast_diff(
        old_sql,
        new_content,
        filename=filename,
        dialect=dialect or "oracle",
    )
    new_parsed = diff["new_parsed_script"]
    new_feats = diff["new_chunk_features"]
    new_agg = aggregate_script_features(new_feats)

    candidate_script_id = target_script.id if target_script is not None else f"candidate:{review_id}"
    candidate_script_dict = {
        "id": candidate_script_id,
        "filename": filename,
        "relative_path": relative_path,
        "file_kind": new_parsed.file_kind,
        "target_tables": new_agg["target_tables"],
        "source_tables": new_agg["source_tables"],
        "commit_count": new_parsed.commit_count,
        "has_exception_handler": new_parsed.has_exception_handler,
    }
    candidate_chunks_dicts = [
        {
            "id": f"cand_chunk_{idx}",
            "script_id": candidate_script_id,
            "chunk_index": ch.chunk_index,
            "start_line": ch.start_line,
            "end_line": ch.end_line,
            "statement_kind": ch.statement_kind,
            "is_dynamic_sql": ch.is_dynamic_sql,
            "control_context": ch.control_context,
            "target_tables": feat.target_tables,
            "source_tables": feat.source_tables,
            "ast_features": feat.to_dict(),
            "ast_fingerprint": feat.ast_fingerprint,
            "raw_sql": ch.raw_sql,
        }
        for idx, (ch, feat) in enumerate(zip(new_parsed.chunks, new_feats))
    ]

    candidate_findings = evaluate_script_rules(candidate_script_dict, candidate_chunks_dicts)

    # Compare baseline findings if MODIFY to highlight incremental findings
    baseline_finding_sigs = set()
    if target_script is not None:
        base_chunks = (
            session.query(ScriptChunkModel)
            .filter(ScriptChunkModel.script_id == target_script.id)
            .all()
        )
        base_findings = evaluate_script_rules(
            {
                "id": target_script.id,
                "filename": target_script.filename,
                "file_kind": target_script.file_kind,
                "target_tables": target_script.target_tables_json or [],
                "source_tables": target_script.source_tables_json or [],
                "commit_count": target_script.commit_count,
                "has_exception_handler": target_script.has_exception_handler,
            },
            [
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
                for c in base_chunks
            ],
        )
        baseline_finding_sigs = {(bf["rule_code"], bf["evidence_snippet"][:120]) for bf in base_findings}

    new_findings: List[Dict[str, Any]] = []
    for cf in candidate_findings:
        sig = (cf["rule_code"], cf["evidence_snippet"][:120])
        is_new = sig not in baseline_finding_sigs
        item = dict(cf)
        item["is_incremental"] = is_new
        if ctype == "ADD" or is_new or cf["severity"] == "P0":
            new_findings.append(item)

    # Simulate project topology WITH candidate change to trace downstream blast radius & new cycles/conflicts
    existing_scripts = session.query(ScriptFileModel).filter(ScriptFileModel.project_id == project_id).all()
    existing_chunks = session.query(ScriptChunkModel).filter(ScriptChunkModel.project_id == project_id).all()
    existing_schemas = session.query(TableSchemaModel).filter(TableSchemaModel.project_id == project_id).all()

    base_script_dicts = [
        {
            "id": s.id,
            "filename": s.filename,
            "relative_path": s.relative_path,
            "file_kind": s.file_kind,
            "target_tables": s.target_tables_json or [],
            "source_tables": s.source_tables_json or [],
        }
        for s in existing_scripts
    ]
    base_chunk_dicts = [
        {
            "id": c.id,
            "script_id": c.script_id,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "statement_kind": c.statement_kind,
            "target_tables": c.target_tables_json or [],
            "source_tables": c.source_tables_json or [],
            "ast_features": c.ast_features_json or {},
            "ast_fingerprint": c.ast_fingerprint,
            "raw_sql": c.raw_sql,
        }
        for c in existing_chunks
    ]
    schema_dicts = [{"table_name": sc.table_name} for sc in existing_schemas]

    base_topology = build_lineage_topology(base_script_dicts, base_chunk_dicts, schema_dicts)

    sim_scripts = [s for s in base_script_dicts if s["id"] != candidate_script_id] + [candidate_script_dict]
    sim_chunks = [c for c in base_chunk_dicts if c["script_id"] != candidate_script_id] + candidate_chunks_dicts
    sim_topology = build_lineage_topology(sim_scripts, sim_chunks, schema_dicts)

    blast_radius = compute_blast_radius(candidate_script_id, sim_topology)

    # Compute newly introduced topology hazards
    base_wc_tables = {wc["table"] for wc in base_topology.get("write_conflict_tables", [])}
    sim_wc_tables = {wc["table"] for wc in sim_topology.get("write_conflict_tables", [])}
    new_write_conflicts = sorted(sim_wc_tables - base_wc_tables)

    base_tc_tables = {tc["table"] for tc in base_topology.get("truncate_conflicts", [])}
    sim_tc_tables = {tc["table"] for tc in sim_topology.get("truncate_conflicts", [])}
    new_truncate_conflicts = sorted(sim_tc_tables - base_tc_tables)

    base_cycles = {tuple(c) for c in base_topology.get("cycles", [])}
    sim_cycles = [c for c in sim_topology.get("cycles", []) if tuple(c) not in base_cycles]

    blast_radius["new_write_conflict_tables"] = new_write_conflicts
    blast_radius["new_truncate_conflict_tables"] = new_truncate_conflicts
    blast_radius["new_cycles"] = sim_cycles

    # Deterministic verdict adjudication
    block_reasons: List[str] = []
    warn_reasons: List[str] = []

    p0_findings = [f for f in new_findings if f["severity"] == "P0" and (ctype == "ADD" or f["is_incremental"])]
    p1_findings = [f for f in new_findings if f["severity"] == "P1" and (ctype == "ADD" or f["is_incremental"])]

    if p0_findings:
        block_reasons.append(f"新增或命中 P0 阻断级缺陷 ({', '.join(sorted({f['rule_code'] for f in p0_findings}))})")
    if diff["exception_handler_removed"]:
        block_reasons.append("修改后移除了原有 EXCEPTION 异常处理保护块")
    if new_truncate_conflicts:
        block_reasons.append(f"触发新的跨脚本 TRUNCATE 并发读取冲突 ({', '.join(new_truncate_conflicts)})")
    if new_write_conflicts:
        block_reasons.append(f"新增主表多脚本并发写冲突 ({', '.join(new_write_conflicts)})")
    if sim_cycles:
        block_reasons.append("引入新的全项目血缘环形依赖 (DAG Cycle)")

    if p1_findings:
        warn_reasons.append(f"新增 P1 性能/规范隐患 ({', '.join(sorted({f['rule_code'] for f in p1_findings}))})")
    if blast_radius["total_impacted_scripts"] >= 1:
        warn_reasons.append(
            f"下游影响波及 {blast_radius['total_impacted_scripts']} 个脚本（最深 {blast_radius['max_hop']} 跳）"
        )
    if diff["removed_target_tables"]:
        warn_reasons.append(f"修改移除了原有目标表写入：{', '.join(diff['removed_target_tables'])}")

    if block_reasons:
        det_verdict = "BLOCK"
    elif warn_reasons:
        det_verdict = "WARN"
    else:
        det_verdict = "PASS"

    diff_summary_json = {
        "unified_diff": diff["unified_diff"],
        "added_lines": diff["added_lines"],
        "removed_lines": diff["removed_lines"],
        "old_line_count": diff["old_line_count"],
        "new_line_count": diff["new_line_count"],
        "old_chunk_count": diff["old_chunk_count"],
        "new_chunk_count": diff["new_chunk_count"],
        "added_target_tables": diff["added_target_tables"],
        "removed_target_tables": diff["removed_target_tables"],
        "added_source_tables": diff["added_source_tables"],
        "removed_source_tables": diff["removed_source_tables"],
        "added_chunks": diff["added_chunks"],
        "removed_chunks": diff["removed_chunks"],
        "exception_handler_removed": diff["exception_handler_removed"],
        "block_reasons": block_reasons,
        "warn_reasons": warn_reasons,
    }

    run = create_run(
        session,
        spec_id=f"review:{filename}",
        initial_node="created",
        initial_payload={
            "project_id": project_id,
            "review_id": review_id,
            "filename": filename,
            "change_type": ctype,
        },
    )
    append_event(
        session,
        run,
        "AST_DIFF_COMPUTED",
        "ast_diff_and_blast",
        {
            "added_lines": diff["added_lines"],
            "removed_lines": diff["removed_lines"],
            "total_impacted_scripts": blast_radius["total_impacted_scripts"],
            "incremental_findings_count": len(new_findings),
        },
    )
    append_event(
        session,
        run,
        "GATE_EVALUATED",
        "deterministic_gate",
        {
            "deterministic_verdict": det_verdict,
            "block_reasons": block_reasons,
            "warn_reasons": warn_reasons,
        },
    )

    ai_review_json: Dict[str, Any] = {"ai_status": "SKIPPED"}
    final_verdict = det_verdict

    if run_llm:
        budget = get_budget(session)
        llm = llm_client or RealDesignLlm(get_effective_settings(session))
        run_bus = build_run_bus(budget=budget, run_id=run.id)
        try:
            with llm_execution_scope(bus=run_bus):
                risk_review, _ = call_llm_validated(
                    llm,
                    "assess_change_risk",
                    instruction=(
                        f"请针对本次 `{ctype}` 变更 (`{filename}`)，结合 Unified Diff、确定性门禁原因及下游爆破半径，"
                        "输出语义回归风险、上线前核对 SQL、历史数据回刷清单与应急回滚方案。"
                    ),
                    payload={
                        "change_type": ctype,
                        "filename": filename,
                        "author": author,
                        "change_summary": change_summary,
                        "deterministic_verdict": det_verdict,
                        "block_reasons": block_reasons,
                        "warn_reasons": warn_reasons,
                        "unified_diff": diff["unified_diff"][:5000],
                        "blast_radius": blast_radius,
                        "incremental_findings": [
                            {
                                "rule_code": f["rule_code"],
                                "severity": f["severity"],
                                "title": f["title"],
                                "description": f["description"][:300],
                            }
                            for f in new_findings[:10]
                        ],
                    },
                    bus=run_bus,
                )
                gate_summary, _ = call_llm_validated(
                    llm,
                    "summarize_release_gate",
                    instruction=(
                        f"基于研发、DBA、测试三视角给出上线门禁总结推荐。注意：确定性门禁为 `{det_verdict}`，"
                        "禁止降级越过硬红线。"
                    ),
                    payload={
                        "filename": filename,
                        "deterministic_verdict": det_verdict,
                        "risk_summary": risk_review.get("risk_summary", ""),
                        "blast_radius": blast_radius,
                    },
                    bus=run_bus,
                )
            ai_verdict = str(gate_summary.get("recommended_verdict") or det_verdict).upper()
            final_verdict = _higher_verdict(det_verdict, ai_verdict)
            guard_snap = run_bus.budget_guard.snapshot() if run_bus.budget_guard else {}
            ai_review_json = {
                "ai_status": "COMPLETED",
                **risk_review,
                **gate_summary,
                "llm_calls": guard_snap.get("llm_calls", 0),
            }
            append_event(
                session,
                run,
                "AI_REVIEW_COMPLETED",
                "ai_risk_review",
                {
                    "deterministic_verdict": det_verdict,
                    "ai_verdict": ai_verdict,
                    "final_verdict": final_verdict,
                },
                bus=run_bus,
            )
        except ProviderAuthOrBillingError as exc:
            ai_review_json = {"ai_status": "BILLING_OR_AUTH_ERROR", "error": str(exc)}
            logger.warning("Change review AI aborted on billing/auth: %s", exc)
        except Exception as exc:
            ai_review_json = {"ai_status": "FAILED_FALLBACK_OFFLINE", "error": str(exc)}
            logger.warning("Change review AI failed cleanly: %s", exc)

    interrupt_payload = make_interrupt_payload(
        "RELEASE_APPROVAL_REQUIRED",
        question=f"请确认是否批准 `{filename}`（{ctype}）变更上线？当前门禁结论：{final_verdict}",
        pending_input={
            "review_id": review_id,
            "filename": filename,
            "change_type": ctype,
            "deterministic_verdict": det_verdict,
            "final_verdict": final_verdict,
            "block_reasons": block_reasons,
            "warn_reasons": warn_reasons,
        },
        resume_nodes={"RELEASE_APPROVAL_REQUIRED": "release_approval_gate"},
    )
    append_event(
        session,
        run,
        "RELEASE_APPROVAL_REQUIRED",
        "release_approval_gate",
        interrupt_payload,
    )

    review = ChangeReviewModel(
        id=review_id,
        project_id=project_id,
        run_id=run.id,
        change_type=ctype,
        target_script_id=target_script.id if target_script else None,
        filename=filename,
        relative_path=relative_path,
        author=author or OPERATOR_PLACEHOLDER,
        change_summary=change_summary,
        old_blob_uri=old_blob_uri,
        new_blob_uri=new_blob_uri,
        diff_summary_json=diff_summary_json,
        blast_radius_json=blast_radius,
        new_findings_json=new_findings,
        deterministic_verdict=det_verdict,
        final_verdict=final_verdict,
        ai_review_json=ai_review_json,
        status="WAITING_APPROVAL",
        merged_to_baseline=False,
    )
    session.add(review)
    session.commit()
    session.refresh(review)

    return serialize_review(review)


def serialize_review(review: ChangeReviewModel, *, include_scripts: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": review.id,
        "project_id": review.project_id,
        "run_id": review.run_id,
        "change_type": review.change_type,
        "target_script_id": review.target_script_id,
        "filename": review.filename,
        "relative_path": review.relative_path,
        "author": review.author,
        "change_summary": review.change_summary,
        "diff_summary": review.diff_summary_json or {},
        "blast_radius": review.blast_radius_json or {},
        "new_findings": review.new_findings_json or [],
        "deterministic_verdict": review.deterministic_verdict,
        "final_verdict": review.final_verdict,
        "ai_review": review.ai_review_json or {},
        "status": review.status,
        "merged_to_baseline": review.merged_to_baseline,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "updated_at": review.updated_at.isoformat() if review.updated_at else None,
    }
    if include_scripts:
        out["old_content"] = blob_store.get_text(review.old_blob_uri) if review.old_blob_uri else ""
        out["new_content"] = blob_store.get_text(review.new_blob_uri) if review.new_blob_uri else ""
    return out
