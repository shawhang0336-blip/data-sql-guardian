"""Interrupt：ProdAgent 通用人工中断与幂等恢复协议微内核。

职责边界（严格零业务硬编码，具体中断名与恢复节点映射位于 app/policy/regops_plan.py）：
- `Interrupt`：一等中断指令值对象，节点通过 `Outcome(interrupt=Interrupt(...))` 声明挂起；
- `make_interrupt_payload`：构造标准四要素中断 payload（question / resume_node / pending_input / interrupt_id）；
- `latest_interrupt` / `pending_interrupt_with` / `matching_answer`：中断查询与指纹匹配原语；
- `answer_interrupt` / `record_interrupt_answer`：统一幂等重放判定、冲突拦截与回答事件落账原语。
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.infra.db_tables import EventModel, RunModel


@dataclass(frozen=True)
class Interrupt:
    """一等人工中断指令：节点声明挂起意图，由 Scheduler 屏障统一落账并停止推进。

    `node` 留空时，Scheduler 会在屏障处自动补齐触发挂起的当前节点名。
    """

    kind: str
    node: str = ""
    question: str = ""
    pending_input: Dict[str, Any] = field(default_factory=dict)
    resume_node: Optional[str] = None
    identity: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.kind or not self.kind.strip():
            raise ValueError("Interrupt.kind 不能为空")

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = dict(self.identity) if isinstance(self.identity, dict) else {}
        # 核心协议字段放在最后覆盖，防止 identity 中的同名键意外污染中断工单
        payload.update(
            {
                "question": self.question,
                "resume_node": self.resume_node or self.node,
                "pending_input": dict(self.pending_input),
                "interrupt_id": uuid.uuid4().hex,
            }
        )
        return payload


def make_interrupt_payload(
    kind: str,
    question: str,
    pending_input: Dict[str, Any],
    resume_nodes: Dict[str, str],
    identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造一条通用 Interrupt 事件的 payload（question / resume_node / pending_input / interrupt_id）。"""
    if kind not in resume_nodes:
        raise ValueError(f"未知中断类型：{kind}")
    payload: Dict[str, Any] = dict(identity) if isinstance(identity, dict) else {}
    payload.update(
        {
            "question": question,
            "resume_node": resume_nodes[kind],
            "pending_input": pending_input,
            "interrupt_id": uuid.uuid4().hex,
        }
    )
    return payload


def matching_answer(
    session: Session,
    run: RunModel,
    answer_kind: str,
    interrupt_id: str,
    answer_fingerprint: str,
) -> Optional[EventModel]:
    """同一中断 + 同样的回答内容 → 认为是一次幂等重入，直接返回原回答事件。"""
    for event in (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == answer_kind)
        .order_by(EventModel.seq)
        .all()
    ):
        payload = event.payload_json or {}
        if payload.get("interrupt_id") == interrupt_id and payload.get("answer_fingerprint") == answer_fingerprint:
            return event
    return None


def latest_interrupt(session: Session, run: RunModel, kind: str) -> Optional[EventModel]:
    """最近一条指定类型的中断事件，不论是否已回答。用于幂等重入判定。"""
    return (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == kind)
        .order_by(EventModel.seq.desc())
        .first()
    )


def pending_interrupt_with(
    session: Session,
    run: RunModel,
    kind: str,
    answer_kinds: Dict[str, str],
) -> Optional[EventModel]:
    """找最近一条尚未被回答的指定类型中断事件。"""
    answer_kind = answer_kinds.get(kind)
    if not answer_kind:
        return None
    answered_ids = {
        (event.payload_json or {}).get("interrupt_id")
        for event in session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == answer_kind)
        .all()
    }
    for event in (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == kind)
        .order_by(EventModel.seq.desc())
        .all()
    ):
        interrupt_id = (event.payload_json or {}).get("interrupt_id")
        if interrupt_id and interrupt_id not in answered_ids:
            return event
    return None


def answer_interrupt(
    session: Session,
    run: RunModel,
    interrupt_kind: str,
    answer_kind: str,
    answer_fingerprint: str,
    *,
    answer_kinds: Optional[Dict[str, str]] = None,
    reject_conflict: bool = True,
) -> Tuple[str, Optional[EventModel]]:
    """通用中断回答幂等解析原语。

    返回值：
    - `("REPLAY", prior_event)`：同一中断且指纹完全一致，幂等重放直接跳过写操作；
    - `("PENDING", interrupt_event)`：存在待回答的中断（或旧数据无 interrupt_id 时的兼容路径），交由调用方落账；
    - 若同一中断已被不同内容回答且 `reject_conflict=True`，抛出 `ValueError`。
    """
    from .event_log import acquire_run_lock

    run_id = run.id
    acquire_run_lock(session, run_id)
    fresh_run = session.get(RunModel, run_id)
    if fresh_run is None:
        raise ValueError("Run not found")
    latest = latest_interrupt(session, fresh_run, interrupt_kind)
    mapping = answer_kinds if answer_kinds is not None else {interrupt_kind: answer_kind}
    pending = pending_interrupt_with(session, fresh_run, interrupt_kind, mapping)
    if latest and (latest.payload_json or {}).get("interrupt_id"):
        interrupt_id = latest.payload_json["interrupt_id"]
        prior = matching_answer(session, fresh_run, answer_kind, interrupt_id, answer_fingerprint)
        if prior:
            return "REPLAY", prior
        # 若同波次还有其他未回答的同类型中断工单（pending is not None），优先交由调用方回答，不误判为冲突
        if pending is None and reject_conflict:
            answered = [
                event
                for event in session.query(EventModel)
                .filter(EventModel.run_id == fresh_run.id, EventModel.kind == answer_kind)
                .all()
                if (event.payload_json or {}).get("interrupt_id") == interrupt_id
            ]
            if answered:
                raise ValueError("This interrupt was already answered with different decisions")
    return "PENDING", pending


def record_interrupt_answer(
    session: Session,
    run: RunModel,
    answer_kind: str,
    node: str,
    payload: Dict[str, Any],
    *,
    interrupt: Optional[EventModel] = None,
    answer_fingerprint: Optional[str] = None,
    attach_interrupt: bool = True,
    bus: Any = None,
) -> EventModel:
    """将中断回答事件标准化落账，自动绑定 `interrupt_id` / `interrupt_seq` / `answer_fingerprint`。"""
    from .event_log import append_event

    answer_payload = dict(payload)
    if attach_interrupt and interrupt is not None:
        interrupt_id = (interrupt.payload_json or {}).get("interrupt_id")
        if interrupt_id:
            answer_payload["interrupt_id"] = interrupt_id
            answer_payload["interrupt_seq"] = interrupt.seq
            if answer_fingerprint is not None:
                answer_payload["answer_fingerprint"] = answer_fingerprint
    return append_event(session, run, answer_kind, node, answer_payload, bus=bus)
