"""Human-in-the-loop release approval / rejection entrypoints, baseline merge, Markdown export, and hash-chain trace."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.analysis import evaluate_script_rules, replace_script_findings
from app.catalog import rebuild_project_lineage, serialize_script
from app.infra.blob_store import blob_store
from app.infra.config import OPERATOR_PLACEHOLDER
from app.infra.db_tables import (
    ChangeReviewModel,
    EventModel,
    ReviewApprovalModel,
    RunModel,
    ScriptChunkModel,
    ScriptFileModel,
    ScriptProjectModel,
    ScriptRevisionModel,
)
from app.ingestion import aggregate_script_features, analyze_chunk_ast, chunk_sql_script
from app.kernel import (
    answer_interrupt,
    build_trace,
    latest_interrupt,
    record_interrupt_answer,
    verify_chain,
)
from .change_evaluator import serialize_review

_APPROVAL_ANSWER_KINDS = {"RELEASE_APPROVAL_REQUIRED": "RELEASE_APPROVED"}
_REJECT_ANSWER_KINDS = {"RELEASE_APPROVAL_REQUIRED": "RELEASE_REJECTED"}


def _decision_fingerprint(decision: str, reviewer: str, comment: str, merge_baseline: bool) -> str:
    mat = f"{decision}|{reviewer}|{comment.strip()}|{int(merge_baseline)}"
    return hashlib.sha256(mat.encode("utf-8")).hexdigest()[:16]


def _merge_review_to_baseline(session: Session, review: ChangeReviewModel, author: str) -> ScriptFileModel:
    """Persist approved candidate SQL into ScriptFileModel, refresh chunks/revisions/lineage/findings."""
    new_content = blob_store.get_text(review.new_blob_uri)
    raw_bytes = new_content.encode("utf-8")
    sha = hashlib.sha256(raw_bytes).hexdigest()

    project = session.get(ScriptProjectModel, review.project_id)
    eff_dialect = None
    if review.target_script_id:
        existing = session.get(ScriptFileModel, review.target_script_id)
    else:
        existing = None

    if existing is not None:
        eff_dialect = existing.dialect
    elif project and project.dialect not in ("auto", ""):
        eff_dialect = project.dialect

    parsed = chunk_sql_script(new_content, filename=review.filename, dialect=eff_dialect)
    feats = [
        analyze_chunk_ast(ch.effective_sql, statement_kind=ch.statement_kind, dialect=parsed.dialect)
        for ch in parsed.chunks
    ]
    agg = aggregate_script_features(feats)

    if existing is None:
        script_row = ScriptFileModel(
            id=str(uuid.uuid4()),
            project_id=review.project_id,
            filename=review.filename,
            relative_path=review.relative_path or review.filename,
            dialect=parsed.dialect,
            file_kind=parsed.file_kind,
            sha256=sha,
            blob_uri=review.new_blob_uri,
            line_count=parsed.line_count,
            chunk_count=len(parsed.chunks),
            target_tables_json=agg["target_tables"],
            source_tables_json=agg["source_tables"],
            ast_fingerprint=agg["ast_fingerprint"],
            has_exception_handler=parsed.has_exception_handler,
            commit_count=parsed.commit_count,
            current_version=1,
            status="ACTIVE",
        )
        session.add(script_row)
        session.flush()
        review.target_script_id = script_row.id
        if project is not None:
            project.script_count = session.query(ScriptFileModel).filter(ScriptFileModel.project_id == project.id).count()
    else:
        script_row = existing
        script_row.dialect = parsed.dialect
        script_row.file_kind = parsed.file_kind
        script_row.sha256 = sha
        script_row.blob_uri = review.new_blob_uri
        script_row.line_count = parsed.line_count
        script_row.chunk_count = len(parsed.chunks)
        script_row.target_tables_json = agg["target_tables"]
        script_row.source_tables_json = agg["source_tables"]
        script_row.ast_fingerprint = agg["ast_fingerprint"]
        script_row.has_exception_handler = parsed.has_exception_handler
        script_row.commit_count = parsed.commit_count
        script_row.current_version = int(script_row.current_version or 1) + 1

    # Replace chunks for this script
    session.execute(delete(ScriptChunkModel).where(ScriptChunkModel.script_id == script_row.id))
    new_chunk_dicts: List[Dict[str, Any]] = []
    for ch, feat in zip(parsed.chunks, feats):
        cid = str(uuid.uuid4())
        session.add(
            ScriptChunkModel(
                id=cid,
                project_id=review.project_id,
                script_id=script_row.id,
                chunk_index=ch.chunk_index,
                start_line=ch.start_line,
                end_line=ch.end_line,
                statement_kind=ch.statement_kind,
                is_dynamic_sql=ch.is_dynamic_sql,
                control_context=ch.control_context,
                target_tables_json=feat.target_tables,
                source_tables_json=feat.source_tables,
                ast_features_json=feat.to_dict(),
                ast_fingerprint=feat.ast_fingerprint,
                raw_sql=ch.raw_sql,
                comments_json=ch.comments,
            )
        )
        new_chunk_dicts.append(
            {
                "id": cid,
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
        )

    session.add(
        ScriptRevisionModel(
            id=str(uuid.uuid4()),
            project_id=review.project_id,
            script_id=script_row.id,
            version=script_row.current_version,
            blob_uri=review.new_blob_uri,
            sha256=sha,
            author=author,
            change_summary=review.change_summary or f"变更评估 {review.id[:8]} 合并基线",
            review_id=review.id,
        )
    )

    if project is not None:
        project.chunk_count = session.query(ScriptChunkModel).filter(ScriptChunkModel.project_id == project.id).count()

    session.commit()
    rebuild_project_lineage(session, review.project_id)

    drafts = evaluate_script_rules(serialize_script(script_row), new_chunk_dicts)
    replace_script_findings(session, project_id=review.project_id, script_id=script_row.id, drafts=drafts)
    return script_row


def approve_change_review(
    session: Session,
    review_id: str,
    *,
    reviewer: str = OPERATOR_PLACEHOLDER,
    comment: str = "",
    merge_baseline: bool = True,
) -> Dict[str, Any]:
    """Approve release gate idempotently via Interrupt protocol and optionally merge candidate into project baseline."""
    review = session.get(ChangeReviewModel, review_id)
    if review is None:
        raise ValueError(f"未找到变更评估单: {review_id}")
    if review.status == "REJECTED":
        raise ValueError("该变更单已被驳回，禁止重复批准")

    run = session.get(RunModel, review.run_id)
    if run is None:
        raise ValueError(f"未找到关联 Run: {review.run_id}")

    fp = _decision_fingerprint("APPROVE", reviewer, comment, merge_baseline)
    mode, pending_evt = answer_interrupt(
        session,
        run,
        "RELEASE_APPROVAL_REQUIRED",
        "RELEASE_APPROVED",
        fp,
        answer_kinds=_APPROVAL_ANSWER_KINDS,
        reject_conflict=True,
    )
    if mode == "REPLAY":
        out = serialize_review(review)
        out["idempotent_replay"] = True
        return out

    interrupt_evt = pending_evt or latest_interrupt(session, run, "RELEASE_APPROVAL_REQUIRED")
    record_interrupt_answer(
        session,
        run,
        "RELEASE_APPROVED",
        "release_approval_gate",
        {
            "decision": "APPROVE",
            "reviewer": reviewer,
            "comment": comment,
            "merge_baseline": merge_baseline,
        },
        interrupt=interrupt_evt,
        answer_fingerprint=fp,
    )
    session.add(
        ReviewApprovalModel(
            id=str(uuid.uuid4()),
            review_id=review.id,
            decision="APPROVE",
            reviewer=reviewer,
            comment=comment,
            merge_baseline=merge_baseline,
        )
    )
    review.status = "APPROVED"
    if merge_baseline and not review.merged_to_baseline:
        _merge_review_to_baseline(session, review, author=reviewer)
        review.merged_to_baseline = True
    session.commit()
    session.refresh(review)
    out = serialize_review(review)
    out["idempotent_replay"] = False
    return out


def reject_change_review(
    session: Session,
    review_id: str,
    *,
    reviewer: str = OPERATOR_PLACEHOLDER,
    comment: str = "",
) -> Dict[str, Any]:
    """Reject release gate idempotently without altering project baseline."""
    review = session.get(ChangeReviewModel, review_id)
    if review is None:
        raise ValueError(f"未找到变更评估单: {review_id}")
    if review.status == "APPROVED":
        raise ValueError("该变更单已批准合并基线，不可驳回")

    run = session.get(RunModel, review.run_id)
    if run is None:
        raise ValueError(f"未找到关联 Run: {review.run_id}")

    fp = _decision_fingerprint("REJECT", reviewer, comment, False)
    mode, pending_evt = answer_interrupt(
        session,
        run,
        "RELEASE_APPROVAL_REQUIRED",
        "RELEASE_REJECTED",
        fp,
        answer_kinds=_REJECT_ANSWER_KINDS,
        reject_conflict=True,
    )
    if mode == "REPLAY":
        out = serialize_review(review)
        out["idempotent_replay"] = True
        return out

    interrupt_evt = pending_evt or latest_interrupt(session, run, "RELEASE_APPROVAL_REQUIRED")
    record_interrupt_answer(
        session,
        run,
        "RELEASE_REJECTED",
        "release_approval_gate",
        {
            "decision": "REJECT",
            "reviewer": reviewer,
            "comment": comment,
            "merge_baseline": False,
        },
        interrupt=interrupt_evt,
        answer_fingerprint=fp,
    )
    session.add(
        ReviewApprovalModel(
            id=str(uuid.uuid4()),
            review_id=review.id,
            decision="REJECT",
            reviewer=reviewer,
            comment=comment,
            merge_baseline=False,
        )
    )
    review.status = "REJECTED"
    session.commit()
    session.refresh(review)
    out = serialize_review(review)
    out["idempotent_replay"] = False
    return out


def list_change_reviews(session: Session, project_id: str) -> List[Dict[str, Any]]:
    rows = (
        session.query(ChangeReviewModel)
        .filter(ChangeReviewModel.project_id == project_id)
        .order_by(ChangeReviewModel.created_at.desc())
        .all()
    )
    return [serialize_review(r) for r in rows]


def get_change_review_detail(session: Session, review_id: str) -> Optional[Dict[str, Any]]:
    review = session.get(ChangeReviewModel, review_id)
    if review is None:
        return None
    out = serialize_review(review, include_scripts=True)
    approvals = (
        session.query(ReviewApprovalModel)
        .filter(ReviewApprovalModel.review_id == review_id)
        .order_by(ReviewApprovalModel.created_at.asc())
        .all()
    )
    out["approvals"] = [
        {
            "id": a.id,
            "decision": a.decision,
            "reviewer": a.reviewer,
            "comment": a.comment,
            "merge_baseline": a.merge_baseline,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in approvals
    ]
    return out


def get_review_run_trace(session: Session, run_id: str) -> Optional[Dict[str, Any]]:
    run = session.get(RunModel, run_id)
    if run is None:
        return None
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run_id)
        .order_by(EventModel.seq.asc())
        .all()
    )
    chain_ok = verify_chain(session, run)
    trace = build_trace(events)
    return {
        "run_id": run.id,
        "spec_id": run.spec_id,
        "chain_verified": chain_ok,
        "broken_seq": None,
        "event_count": len(events),
        "events": [
            {
                "seq": e.seq,
                "kind": e.kind,
                "node": e.node,
                "payload": e.payload_json or {},
                "event_hash": e.event_hash,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
        "spans": [sp.as_dict() for sp in trace.spans],
    }


def export_review_markdown(session: Session, review_id: str) -> str:
    detail = get_change_review_detail(session, review_id)
    if detail is None:
        raise ValueError(f"未找到变更评估单: {review_id}")
    diff = detail.get("diff_summary") or {}
    blast = detail.get("blast_radius") or {}
    ai = detail.get("ai_review") or {}

    lines: List[str] = [
        f"# 上线前脚本变更评估报告：`{detail['filename']}`",
        "",
        f"- **变更单号**：`{detail['id']}`",
        f"- **变更类型**：`{detail['change_type']}`",
        f"- **提交人**：{detail['author']}",
        f"- **当前状态**：`{detail['status']}`（已合并基线：{detail['merged_to_baseline']}）",
        f"- **确定性门禁**：**{detail['deterministic_verdict']}**",
        f"- **最终准入裁决**：**{detail['final_verdict']}**",
        "",
        "## 1. 门禁裁决原因",
    ]
    for r in diff.get("block_reasons") or []:
        lines.append(f"- [BLOCK] {r}")
    for r in diff.get("warn_reasons") or []:
        lines.append(f"- [WARN] {r}")
    if not diff.get("block_reasons") and not diff.get("warn_reasons"):
        lines.append("- 未触发任何确定性风险拦截或告警。")

    lines.extend(
        [
            "",
            "## 2. 结构差异与下游影响面（Blast Radius）",
            f"- 代码行数变化：`+{diff.get('added_lines', 0)}` / `-{diff.get('removed_lines', 0)}`",
            f"- 目标写入表：`{', '.join(diff.get('added_target_tables', [])) or '无新增'}`",
            f"- 下游波及脚本总数：**{blast.get('total_impacted_scripts', 0)}**（最大跳数 {blast.get('max_hop', 0)}）",
            "",
        ]
    )
    if blast.get("impacted_scripts"):
        lines.append("| 跳数 | 下游脚本 | 关联数据表 | 依赖类型 |")
        lines.append("| --- | --- | --- | --- |")
        for s in blast["impacted_scripts"]:
            lines.append(f"| H{s['hop']} | `{s['filename']}` | `{s['via_table']}` | {s['edge_kind']} |")
        lines.append("")

    lines.append("## 3. 增量静态扫描问题清单")
    findings = detail.get("new_findings") or []
    if not findings:
        lines.append("- 无新增静态规则告警。")
    else:
        for f in findings:
            lines.append(f"- **[{f['severity']}][{f['rule_code']}]** {f['title']} (L{f.get('start_line')}-{f.get('end_line')})")
            lines.append(f"  - 说明：{f['description']}")
            lines.append(f"  - 建议：{f['suggestion']}")

    if ai.get("ai_status") == "COMPLETED":
        lines.extend(
            [
                "",
                "## 4. AI 上线审查、核对 SQL 与回滚预案",
                f"- **风险评估**：{ai.get('risk_summary', '')}",
                f"- **研发视角**：{ai.get('developer_notes', '')}",
                f"- **DBA 视角**：{ai.get('dba_notes', '')}",
                f"- **QA 视角**：{ai.get('qa_notes', '')}",
                f"- **回滚方案**：{ai.get('rollback_plan', '')}",
            ]
        )
        if ai.get("verification_sqls"):
            lines.append("")
            lines.append("### 上线后核对 SQL")
            for sql in ai["verification_sqls"]:
                lines.append("```sql")
                lines.append(sql)
                lines.append("```")

    return "\n".join(lines)
