"""ds_agent 领域工作流蓝图与显式事件状态折叠策略（策略层 Layer 2，零 import 期全局副作用）。"""

from dataclasses import replace
from typing import Any, Dict, Iterable, Tuple

from sqlalchemy.orm import Session

from app.infra.db_tables import RunModel
from app.kernel.event_log import RunState, fold_with, project_many_with, project_with
from app.kernel.plan import PlanEdge, PlanNode, WorkflowPlan

# ── 1. 全项目分析与脚本优化拓扑 ──────────────────────────────────────────────────

PROJECT_ANALYSIS_NODES: Tuple[PlanNode, ...] = (
    PlanNode("created", "已创建任务", "stage"),
    PlanNode("static_scan", "切块与静态规则体检", "stage"),
    PlanNode("chunk_fanout", "切块级 AI 深度诊断与 SQL 改写", "stage"),
    PlanNode("project_synthesis", "全项目架构重构建议合成", "stage"),
    PlanNode("completed", "优化报告已就绪", "terminal"),
    PlanNode("failed", "失败", "terminal"),
)

PROJECT_ANALYSIS_EDGES: Tuple[PlanEdge, ...] = (
    PlanEdge("created", "static_scan", "always"),
    PlanEdge("static_scan", "chunk_fanout", "always"),
    PlanEdge("chunk_fanout", "project_synthesis", "always"),
    PlanEdge("project_synthesis", "completed", "always"),
    PlanEdge("project_synthesis", "failed", "failed"),
)

PROJECT_ANALYSIS_PLAN = WorkflowPlan(
    name="ds_project_analysis",
    nodes=PROJECT_ANALYSIS_NODES,
    edges=PROJECT_ANALYSIS_EDGES,
    entry="created",
)


# ── 2. 新增 / 修改脚本上线前变更评估与人工准入拓扑 ─────────────────────────────────

CHANGE_REVIEW_NODES: Tuple[PlanNode, ...] = (
    PlanNode("created", "提交变更评估", "stage"),
    PlanNode("ast_diff_and_blast", "AST 差异比对与爆破半径计算", "stage"),
    PlanNode("deterministic_gate", "上线准入确定性门禁裁决", "stage"),
    PlanNode("ai_risk_review", "AI 风险评估与验证回滚建议", "stage"),
    PlanNode("release_approval_gate", "上线前人工审批门禁", "gate"),
    PlanNode("approved", "批准上线", "terminal"),
    PlanNode("rejected", "驳回退改", "terminal"),
    PlanNode("failed", "评估失败", "terminal"),
)

CHANGE_REVIEW_EDGES: Tuple[PlanEdge, ...] = (
    PlanEdge("created", "ast_diff_and_blast", "always"),
    PlanEdge("ast_diff_and_blast", "deterministic_gate", "always"),
    PlanEdge("deterministic_gate", "ai_risk_review", "always"),
    PlanEdge("ai_risk_review", "release_approval_gate", "needs_approval"),
    PlanEdge("release_approval_gate", "approved", "approve"),
    PlanEdge("release_approval_gate", "rejected", "reject"),
    PlanEdge("ai_risk_review", "failed", "failed"),
)

CHANGE_REVIEW_PLAN = WorkflowPlan(
    name="ds_pre_release_review",
    nodes=CHANGE_REVIEW_NODES,
    edges=CHANGE_REVIEW_EDGES,
    entry="created",
)


def _apply(state: RunState, kind: str, payload: Dict[str, Any]) -> RunState:
    payload = payload or {}
    if kind == "RUN_CREATED":
        return replace(state, status="RUNNING", current_node="created", when="always")
    if kind == "STATIC_SCANNED":
        return replace(state, status="RUNNING", current_node="static_scan", result_json=payload)
    if kind == "CHUNK_ANALYZED":
        return replace(state, status="RUNNING", current_node="chunk_fanout", result_json=payload)
    if kind == "PROJECT_SYNTHESIZED":
        return replace(state, status="COMPLETED", current_node="completed", result_json=payload)
    if kind == "AST_DIFF_COMPUTED":
        return replace(state, status="RUNNING", current_node="ast_diff_and_blast", result_json=payload)
    if kind == "GATE_EVALUATED":
        return replace(state, status="RUNNING", current_node="deterministic_gate", result_json=payload)
    if kind == "AI_REVIEW_COMPLETED":
        return replace(state, status="RUNNING", current_node="ai_risk_review", when="needs_approval", result_json=payload)
    if kind == "RELEASE_APPROVAL_REQUIRED":
        return replace(
            state,
            status="WAITING_HUMAN",
            current_node="release_approval_gate",
            when="approve",
            resume_from_node="release_approval_gate",
            resume_when="approve",
            result_json=payload,
        )
    if kind == "RELEASE_APPROVED":
        return replace(state, status="COMPLETED", current_node="approved", result_json=payload)
    if kind == "RELEASE_REJECTED":
        return replace(state, status="REJECTED", current_node="rejected", result_json=payload)
    if kind == "RUN_CANCELLED":
        return replace(state, status="CANCELLED", current_node="failed", result_json=payload)
    if kind == "RUN_FAILED":
        return replace(state, status="FAILED", current_node="failed", result_json=payload)
    return state


def fold(events) -> RunState:
    return fold_with(events, reducer=_apply)


def project(session: Session, run: RunModel) -> RunState:
    return project_with(session, run, reducer=_apply)


def project_many(session: Session, runs: Iterable[RunModel]) -> Dict[str, RunState]:
    return project_many_with(session, runs, reducer=_apply)
