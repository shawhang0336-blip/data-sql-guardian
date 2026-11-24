"""ds_agent BSP 命名数据通道声明、上下文装配与中断恢复还原策略（策略层 Layer 2）。"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.infra.config import OPERATOR_PLACEHOLDER, Settings, get_settings
from app.infra.db_tables import EventModel, RunModel
from app.kernel.bus import Bus
from app.kernel.run import ChannelSpec, Identity, RunContext, append_list, last_value, merge_dict
from .ds_bus import build_run_bus
from .ds_plan import fold, project

PROJECT_ANALYSIS_CHANNELS: Dict[str, ChannelSpec] = {
    "script_findings": ChannelSpec("script_findings", append_list),
    "chunk_diagnostics": ChannelSpec("chunk_diagnostics", merge_dict),
    "project_findings": ChannelSpec("project_findings", append_list),
    "ai_roadmap": ChannelSpec("ai_roadmap", last_value),
}

CHANGE_REVIEW_CHANNELS: Dict[str, ChannelSpec] = {
    "diff_summary": ChannelSpec("diff_summary", merge_dict),
    "blast_radius": ChannelSpec("blast_radius", last_value),
    "new_findings": ChannelSpec("new_findings", last_value),
    "gate_result": ChannelSpec("gate_result", merge_dict),
    "ai_review": ChannelSpec("ai_review", last_value),
}


def recover_change_review_channels(
    session: Session,
    run: RunModel,
    events: Optional[List[EventModel]] = None,
) -> Dict[str, Any]:
    """从 EventLog 折叠还原变更评估 Run 的共享通道快照，供审批中断恢复时复用。"""
    if events is None:
        events = (
            session.query(EventModel)
            .filter(EventModel.run_id == run.id)
            .order_by(EventModel.seq.asc())
            .all()
        )
    shared: Dict[str, Any] = {
        "diff_summary": {},
        "gate_result": {},
    }
    for event in events:
        kind = event.kind
        payload = event.payload_json or {}
        if kind == "AST_DIFF_COMPUTED":
            shared["diff_summary"] = merge_dict(
                shared.get("diff_summary"),
                {
                    k: payload[k]
                    for k in ("added_lines", "removed_lines", "total_impacted_scripts", "incremental_findings_count")
                    if k in payload
                },
            )
            if "blast_radius" in payload:
                shared["blast_radius"] = payload["blast_radius"]
        elif kind == "GATE_EVALUATED":
            reason_patch = {k: payload[k] for k in ("block_reasons", "warn_reasons") if k in payload}
            if reason_patch:
                shared["diff_summary"] = merge_dict(shared.get("diff_summary"), reason_patch)
            gate_patch = {
                k: payload[k]
                for k in ("deterministic_verdict", "block_reasons", "warn_reasons")
                if k in payload
            }
            if "deterministic_verdict" in payload:
                gate_patch.setdefault("final_verdict", payload["deterministic_verdict"])
            if gate_patch:
                shared["gate_result"] = merge_dict(shared.get("gate_result"), gate_patch)
        elif kind == "AI_REVIEW_COMPLETED":
            shared["ai_review"] = {"ai_status": "COMPLETED", **payload}
            gate_patch = {
                k: payload[k]
                for k in ("deterministic_verdict", "ai_verdict", "final_verdict")
                if k in payload
            }
            if gate_patch:
                shared["gate_result"] = merge_dict(shared.get("gate_result"), gate_patch)
        elif kind == "RELEASE_APPROVAL_REQUIRED":
            pending = payload.get("pending_input") if isinstance(payload.get("pending_input"), dict) else {}
            if pending:
                reason_patch = {k: pending[k] for k in ("block_reasons", "warn_reasons") if k in pending}
                if reason_patch:
                    shared["diff_summary"] = merge_dict(shared.get("diff_summary"), reason_patch)
                gate_patch = {
                    k: pending[k]
                    for k in ("deterministic_verdict", "final_verdict", "block_reasons", "warn_reasons")
                    if k in pending
                }
                if gate_patch:
                    shared["gate_result"] = merge_dict(shared.get("gate_result"), gate_patch)
        elif kind in {"RELEASE_APPROVED", "RELEASE_REJECTED"}:
            gate_patch = {
                k: payload[k]
                for k in ("decision", "reviewer", "comment", "merge_baseline")
                if k in payload
            }
            if gate_patch:
                shared["gate_result"] = merge_dict(shared.get("gate_result"), gate_patch)
    return shared


def build_change_review_context(
    session: Session,
    run: RunModel,
    *,
    settings: Optional[Settings] = None,
    llm: Any = None,
    budget: Optional[Dict[str, Any]] = None,
    bus: Optional[Bus] = None,
    concurrency: int = 4,
    operator: str = OPERATOR_PLACEHOLDER,
    approver: Optional[str] = None,
) -> RunContext:
    """组装变更评估与审批恢复所需的 `RunContext`（单次查询 EventModel 同步折叠状态与通道）。"""
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq.asc())
        .all()
    )
    state = fold(events)
    shared = recover_change_review_channels(session, run, events=events)
    active_bus = bus if bus is not None else build_run_bus(budget=budget, run_id=run.id)
    return RunContext(
        session=session,
        run=run,
        settings=settings or get_settings(),
        state=state,
        identity=Identity(operator=operator or OPERATOR_PLACEHOLDER, approver=approver),
        shared=shared,
        channels=dict(CHANGE_REVIEW_CHANNELS),
        bus=active_bus,
        llm=llm,
        budget=budget,
        concurrency=concurrency,
        current_node=state.current_node,
        projector=project,
        budget_guard=getattr(active_bus, "budget_guard", None),
    )


def build_analysis_context(
    session: Session,
    run: RunModel,
    *,
    settings: Optional[Settings] = None,
    llm: Any = None,
    budget: Optional[Dict[str, Any]] = None,
    bus: Optional[Bus] = None,
    concurrency: int = 4,
) -> RunContext:
    """组装单脚本/全仓分析所需的 `RunContext`（挂载 `PROJECT_ANALYSIS_CHANNELS` 与门禁总线）。"""
    state = project(session, run)
    active_bus = bus if bus is not None else build_run_bus(budget=budget, run_id=run.id)
    return RunContext(
        session=session,
        run=run,
        settings=settings or get_settings(),
        state=state,
        shared={},
        channels=dict(PROJECT_ANALYSIS_CHANNELS),
        bus=active_bus,
        llm=llm,
        budget=budget,
        concurrency=concurrency,
        current_node=state.current_node,
        projector=project,
        budget_guard=getattr(active_bus, "budget_guard", None),
    )
