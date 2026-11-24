"""EventLog：ProdAgent 事件溯源账本微内核（唯一事实源）。

职责边界：
- RunModel 只保存身份（id/spec_id/时间戳）；
- 状态（RunState）由 append-only 的事件账本通过可插拔 StateReducer 纯函数折叠产生；
- 每条事件串联 SHA-256 哈希链（prev_hash -> event_hash），支持防篡改校验（verify_chain）；
- 提供通用 Interrupt 协议三要素构造与幂等重入查询原语；
- 机制层不硬编码任何业务事件名（业务折叠规则位于 app/policy/regops_plan.py）。
"""

import gc
import hashlib
import json
import threading
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from app.infra.db_tables import EventModel, RunModel
from .bus import global_bus

GENESIS_HASH = "0" * 64


_MYSQL_JSON_INT_MIN = -(1 << 63)
_MYSQL_JSON_INT_MAX = (1 << 64) - 1


def _normalize_event_value(value: Any) -> Any:
    """对齐 MySQL 8 JSON 列数值表示：超出 64 位整数范围的超大 int 在落库后会被 MySQL 转为 DOUBLE。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < _MYSQL_JSON_INT_MIN or value > _MYSQL_JSON_INT_MAX:
            return float(value)
        return value
    if isinstance(value, dict):
        return {k: _normalize_event_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_event_value(v) for v in value]
    if isinstance(value, tuple):
        return [_normalize_event_value(v) for v in value]
    return value


def canonical_event_json(value: Any) -> str:
    """确定性 JSON 序列化（按 key 排序、紧凑分隔符），用于计算事件 SHA-256 哈希链。"""
    return json.dumps(_normalize_event_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RunState:
    """Run 在某一事件序号处的状态。纯值对象，不落库。"""

    status: str = "NEW"
    current_node: str = "created"
    attempt: int = 0
    result_json: Optional[Dict[str, Any]] = None
    when: str = "always"
    pending_target: Optional[str] = None
    resume_from_node: str = "created"
    resume_when: str = "always"


StateReducer = Callable[[RunState, str, Dict[str, Any]], RunState]

_DEFAULT_REDUCER: StateReducer = lambda state, _kind, _payload: state


def register_default_reducer(reducer: StateReducer) -> None:
    """由策略层注册默认事件折叠器（如 RegOps 的 _apply）。"""
    global _DEFAULT_REDUCER
    _DEFAULT_REDUCER = reducer


def fold_with(events: Iterable[EventModel], reducer: Optional[StateReducer] = None) -> RunState:
    """按 seq 顺序折叠事件流，得到当前状态。纯函数，同一批事件永远得到同一结果。"""
    active_reducer = reducer or _DEFAULT_REDUCER
    state = RunState()
    for event in events:
        state = active_reducer(state, event.kind, event.payload_json)
    return state


def _state_cache(session: Session) -> Dict[str, Dict[int, RunState]]:
    """session 级投影缓存（按 run_id 与 reducer 隔离）。同一请求内多次读取不重复折叠；append_event 或 rollback 会自动失效。"""
    return session.info.setdefault("_run_state_cache", {})


class _SessionRLock:
    """支持跨线程 GC/finalizer 安全解锁的事务级可重入锁。

    标准 `threading.RLock` 仅允许持锁线程调用 `release()`，若调用方创建内存 `Session` 后
    未显式 `commit()/close()` 即离开作用域，GC 在其他线程触发清理时无法放锁；本锁在同一线程内
    保持可重入，同时允许 `weakref.finalize` 在任何线程回收失联 `Session` 时安全释放。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._owner: Optional[int] = None
        self._count = 0

    def acquire(self, blocking: bool = True) -> bool:
        tid = threading.get_ident()
        with self._cond:
            if self._owner == tid:
                self._count += 1
                return True
            if not blocking and self._count > 0:
                return False
            while self._count > 0:
                self._cond.wait()
            self._owner = tid
            self._count = 1
            return True

    def acquire_if_idle(self) -> bool:
        with self._cond:
            if self._count > 0:
                return False
            self._owner = threading.get_ident()
            self._count = 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._count <= 0:
                return
            self._count -= 1
            if self._count == 0:
                self._owner = None
                self._cond.notify()


def _release_lock_box(box: List[Any]) -> None:
    if not box:
        return
    lock = box.pop()
    lock.release()


def _acquire_with_gc(lock: _SessionRLock) -> None:
    if not lock.acquire(blocking=False):
        gc.collect()
        lock.acquire()


def bind_session_lock(
    session: Session,
    lock: _SessionRLock,
    bucket_key: str,
    entry_key: str = "_",
) -> None:
    """将进程级可重入锁绑定到当前 `Session` 事务生命周期，并在 GC 回收时兜底放锁。"""
    held = session.info.get(bucket_key)
    if held and entry_key in held:
        return
    _acquire_with_gc(lock)
    try:
        has_unflushed = bool(session.new or session.dirty or session.deleted)
        if session.in_transaction() and not session.info.get("_tx_has_writes") and not has_unflushed:
            saved = dict(session.info.pop(bucket_key, {}))
            session.rollback()
            box = [lock]
            saved[entry_key] = box
            session.info[bucket_key] = saved
        else:
            if not has_unflushed:
                session.expire_all()
            box = [lock]
            session.info.setdefault(bucket_key, {})[entry_key] = box
        weakref.finalize(session, _release_lock_box, box)
    except Exception:
        lock.release()
        raise


def release_session_locks(session: Session, bucket_key: str) -> None:
    """释放某个 `Session` 在指定槽位上持有的全部锁。"""
    held = session.info.pop(bucket_key, None)
    if held:
        for box in reversed(list(held.values())):
            _release_lock_box(box)


_MAX_RUN_LOCKS = 2048
_RUN_LOCKS_GUARD = threading.Lock()
_RUN_LOCKS: Dict[str, _SessionRLock] = {}


def _get_run_lock(run_id: str) -> _SessionRLock:
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(run_id)
        if lock is not None:
            return lock
        if len(_RUN_LOCKS) >= _MAX_RUN_LOCKS:
            for key in list(_RUN_LOCKS.keys()):
                candidate = _RUN_LOCKS[key]
                if candidate.acquire_if_idle():
                    try:
                        _RUN_LOCKS.pop(key, None)
                    finally:
                        candidate.release()
                    if len(_RUN_LOCKS) < _MAX_RUN_LOCKS:
                        break
        lock = _SessionRLock()
        _RUN_LOCKS[run_id] = lock
        return lock


def _release_held_run_locks(session: Session) -> None:
    release_session_locks(session, "_held_run_locks")
    session.info.pop("_tx_has_writes", None)


@sa_event.listens_for(Session, "after_flush")
def _mark_session_writes(session: Session, _flush_context: Any) -> None:
    session.info["_tx_has_writes"] = True


@sa_event.listens_for(Session, "do_orm_execute")
def _mark_orm_writes(orm_execute_state: Any) -> None:
    if orm_execute_state.is_update or orm_execute_state.is_delete:
        orm_execute_state.session.info["_tx_has_writes"] = True


@sa_event.listens_for(Session, "after_soft_rollback")
def _clear_state_cache_on_rollback(session: Session, _previous_transaction: Any) -> None:
    """事务回滚后立即清空 session 级状态投影缓存，杜绝读到已回滚的幽灵脏状态。"""
    session.info.pop("_run_state_cache", None)
    if not session.in_transaction():
        _release_held_run_locks(session)


@sa_event.listens_for(Session, "after_transaction_end")
def _release_locks_on_tx_end(session: Session, transaction: Any) -> None:
    # 仅在最外层根事务真正结束时释放锁；begin_nested() 保存点结束也会触发 after_commit，绝不能在此前提前放锁。
    if getattr(transaction, "parent", None) is None and not session.in_transaction():
        _release_held_run_locks(session)


def acquire_run_lock(session: Session, run_id: str) -> None:
    """按 run_id 在当前事务生命周期内串行化状态检查与事件追加，杜绝并发写入导致哈希链分叉或重入。"""
    if not run_id:
        return
    held = session.info.get("_held_run_locks")
    if held and run_id in held:
        return
    lock = _get_run_lock(run_id)
    _state_cache(session).pop(run_id, None)
    bind_session_lock(session, lock, "_held_run_locks", run_id)


def project_with(
    session: Session,
    run: RunModel,
    reducer: Optional[StateReducer] = None,
) -> RunState:
    """查询并折叠某个 Run 的事件账本。所有状态读取的唯一权威入口。"""
    active_reducer = reducer or _DEFAULT_REDUCER
    reducer_key = id(active_reducer)
    cache = _state_cache(session)
    by_reducer = cache.get(run.id)
    if by_reducer is not None and reducer_key in by_reducer:
        return by_reducer[reducer_key]
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    state = fold_with(events, reducer=active_reducer)
    cache.setdefault(run.id, {})[reducer_key] = state
    return state


def project_many_with(
    session: Session,
    runs: Iterable[RunModel],
    reducer: Optional[StateReducer] = None,
) -> Dict[str, RunState]:
    """批量折叠：一次 IN 查询取出所有事件，避免列表接口的 N+1。"""
    ids = [run.id for run in runs]
    if not ids:
        return {}
    active_reducer = reducer or _DEFAULT_REDUCER
    reducer_key = id(active_reducer)
    cache = _state_cache(session)
    if all(reducer_key in cache.get(run_id, {}) for run_id in ids):
        return {run_id: cache[run_id][reducer_key] for run_id in ids}
    grouped: Dict[str, List[EventModel]] = defaultdict(list)
    rows = (
        session.query(EventModel)
        .filter(EventModel.run_id.in_(ids))
        .order_by(EventModel.run_id, EventModel.seq)
        .all()
    )
    for event in rows:
        grouped[event.run_id].append(event)
    states = {run_id: fold_with(grouped.get(run_id, []), reducer=active_reducer) for run_id in ids}
    for run_id, state in states.items():
        cache.setdefault(run_id, {})[reducer_key] = state
    return states


def compute_event_hash(
    run_id: str,
    kind: str,
    node: str,
    payload: Dict[str, Any],
    prev_hash: str,
) -> str:
    material = canonical_event_json(
        {
            "run_id": run_id,
            "kind": kind,
            "node": node,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_event(
    session: Session,
    run: RunModel,
    kind: str,
    node: str,
    payload: Dict[str, Any],
    bus: Any = None,
) -> EventModel:
    """追加一条不可变事件，并通过 Bus 广播 event.appended 生命周期信号。"""
    run_id = run.id
    acquire_run_lock(session, run_id)
    # 快照化并按 canonical_event_json 归一化载荷：防止调用方事后修改原 dict 导致已计算的 SHA-256 哈希链断裂
    stored_payload = json.loads(canonical_event_json(payload if payload is not None else {}))
    previous = (
        session.query(EventModel)
        .filter(EventModel.run_id == run_id)
        .order_by(EventModel.seq.desc())
        .first()
    )
    prev_hash = previous.event_hash if previous else GENESIS_HASH
    event_hash = compute_event_hash(run_id, kind, node, stored_payload, prev_hash)
    event = EventModel(
        run_id=run_id,
        kind=kind,
        node=node,
        payload_json=stored_payload,
        prev_hash=prev_hash,
        event_hash=event_hash,
    )
    session.add(event)
    session.query(RunModel).filter(RunModel.id == run_id).update(
        {RunModel.updated_at: datetime.utcnow()}, synchronize_session=False
    )
    session.flush()
    _state_cache(session).pop(run_id, None)

    active_bus = bus if bus is not None else global_bus()
    if hasattr(active_bus, "fire"):
        active_bus.fire(
            "event.appended",
            {
                "run_id": run_id,
                "seq": event.seq,
                "kind": kind,
                "node": node,
                "event_hash": event.event_hash,
            },
        )
        if kind == "RUN_CREATED":
            active_bus.fire("run.created", {"run_id": run_id, "spec_id": run.spec_id, "node": node})
        elif kind in {"RUN_RESUMED", "REAUDIT_REQUESTED"}:
            active_bus.fire("run.resumed", {"run_id": run_id, "kind": kind, "node": node})
    return event


def verify_chain(session: Session, run: RunModel) -> bool:
    """校验事件账本的哈希链完整性（防篡改 / 检测手工删改）。"""
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    prev_hash = GENESIS_HASH
    for event in events:
        if event.prev_hash != prev_hash:
            return False
        prev_hash = compute_event_hash(run.id, event.kind, event.node, event.payload_json, prev_hash)
        if event.event_hash != prev_hash:
            return False
    return True


# ── 事件流零成本 Trace / Span 折叠（不新增表，从同一份 EventLog 投影耗时瀑布） ─────────


def _event_timestamp_seconds(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, datetime):
        return raw.timestamp()
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _event_iso(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.isoformat()
    if isinstance(raw, str):
        return raw
    return None


def _event_reported_ms(payload: Dict[str, Any]) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    if payload.get("duration_ms") is not None:
        try:
            return max(0, round(float(payload["duration_ms"])))
        except (TypeError, ValueError):
            pass
    if payload.get("elapsed_seconds") is not None:
        try:
            return max(0, round(float(payload["elapsed_seconds"]) * 1000))
        except (TypeError, ValueError):
            pass
    return None


@dataclass
class TraceSpan:
    """单个节点/分片在 EventLog 中的一次连续执行跨度。"""

    node: str
    label: str
    shard: Optional[str] = None
    seq_start: int = 0
    seq_end: int = 0
    event_count: int = 0
    kinds: List[str] = field(default_factory=list)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: int = 0
    wall_ms: int = 0
    reported_ms: Optional[int] = None
    status: str = "ok"  # ok | error | interrupted | cancelled | retried
    retries: int = 0
    rejections: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "label": self.label,
            "shard": self.shard,
            "seq_start": self.seq_start,
            "seq_end": self.seq_end,
            "event_count": self.event_count,
            "kinds": list(self.kinds),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "wall_ms": self.wall_ms,
            "reported_ms": self.reported_ms,
            "status": self.status,
            "retries": self.retries,
            "rejections": self.rejections,
        }


@dataclass
class RunTrace:
    """单次 Run 的因果与耗时追踪视图（纯函数从 EventLog 折叠得到）。"""

    run_id: str
    status: str = "running"  # ok | error | interrupted | cancelled | running
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    total_duration_ms: int = 0
    event_count: int = 0
    retry_count: int = 0
    rejection_count: int = 0
    interrupt_count: int = 0
    spans: List[TraceSpan] = field(default_factory=list)
    by_node: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total_duration_ms": self.total_duration_ms,
            "event_count": self.event_count,
            "retry_count": self.retry_count,
            "rejection_count": self.rejection_count,
            "interrupt_count": self.interrupt_count,
            "spans": [span.as_dict() for span in self.spans],
            "by_node": {k: dict(v) for k, v in self.by_node.items()},
        }


def _plan_node_label(plan: Any, node_name: str) -> str:
    if plan is not None and hasattr(plan, "node"):
        try:
            plan_node = plan.node(node_name)
            label = getattr(plan_node, "label", None)
            if label:
                return str(label)
        except Exception:
            pass
    return node_name


def build_trace(
    events: Iterable[Any],
    *,
    run_id: str = "",
    plan: Any = None,
) -> RunTrace:
    """将 EventLog 事件序列折叠为结构化耗时 Span 视图（零业务硬编码、零额外存储）。"""
    items = list(events)
    if not items:
        return RunTrace(run_id=run_id)

    def _get(ev: Any, key: str, default: Any = None) -> Any:
        if isinstance(ev, dict):
            return ev.get(key, default)
        return getattr(ev, key, default)

    resolved_run_id = run_id or str(_get(items[0], "run_id", "") or "")
    trace = RunTrace(run_id=resolved_run_id, event_count=len(items))

    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    prev_span_end_ts: Optional[float] = None
    current_span: Optional[TraceSpan] = None
    current_key: Optional[Tuple[str, Optional[str]]] = None
    current_first_ts: Optional[float] = None
    current_last_ts: Optional[float] = None
    current_reported_sum = 0
    current_has_reported = False

    def _finalize_span() -> None:
        nonlocal prev_span_end_ts
        if current_span is None:
            return
        anchor_ts = prev_span_end_ts if prev_span_end_ts is not None else current_first_ts
        if anchor_ts is not None and current_last_ts is not None:
            current_span.wall_ms = max(0, round((current_last_ts - anchor_ts) * 1000))
        if current_has_reported:
            current_span.reported_ms = current_reported_sum
            current_span.duration_ms = current_reported_sum
        else:
            current_span.duration_ms = current_span.wall_ms
        trace.spans.append(current_span)
        if current_last_ts is not None:
            prev_span_end_ts = current_last_ts

    for idx, ev in enumerate(items, start=1):
        seq = int(_get(ev, "seq", idx) or idx)
        kind = str(_get(ev, "kind", "") or "")
        node = str(_get(ev, "node", "") or "unknown")
        payload = _get(ev, "payload_json")
        if payload is None and isinstance(ev, dict):
            payload = ev.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        created_raw = _get(ev, "created_at")
        ts = _event_timestamp_seconds(created_raw)
        iso = _event_iso(created_raw)

        if ts is not None:
            if first_ts is None:
                first_ts = ts
                trace.started_at = iso
            last_ts = ts
            trace.ended_at = iso

        shard_raw = payload.get("label") or payload.get("table_code")
        shard = str(shard_raw) if isinstance(shard_raw, str) and shard_raw else None
        span_key = (node, shard)

        if current_span is None or span_key != current_key:
            _finalize_span()
            current_key = span_key
            current_span = TraceSpan(
                node=node,
                label=_plan_node_label(plan, node),
                shard=shard,
                seq_start=seq,
                seq_end=seq,
                started_at=iso,
                ended_at=iso,
            )
            current_first_ts = ts
            current_last_ts = ts
            current_reported_sum = 0
            current_has_reported = False

        current_span.seq_end = seq
        current_span.event_count += 1
        current_span.kinds.append(kind)
        if iso is not None:
            current_span.ended_at = iso
        if ts is not None:
            if current_first_ts is None:
                current_first_ts = ts
                current_span.started_at = iso
            current_last_ts = ts

        rep_ms = _event_reported_ms(payload)
        if rep_ms is not None:
            current_reported_sum += rep_ms
            current_has_reported = True

        if kind == "NODE_RETRY":
            current_span.retries += 1
            trace.retry_count += 1
            if current_span.status == "ok":
                current_span.status = "retried"
        elif kind == "BUS_REJECTED":
            current_span.rejections += 1
            trace.rejection_count += 1
        elif kind == "RUN_CANCELLED":
            current_span.status = "cancelled"
            trace.status = "cancelled"
        elif kind in {"RUN_FAILED", "AUDIT_REJECTED"} or kind.endswith("_FAILED"):
            current_span.status = "error"
            trace.status = "error"
        elif kind.endswith("_REQUIRED") or (
            isinstance(payload.get("interrupt_id"), str) and not kind.endswith(("_RECORDED", "_CONFIRMED", "_ANSWERED"))
        ):
            current_span.status = "interrupted"
            trace.interrupt_count += 1
            trace.status = "interrupted"
        elif kind in {"RUN_RESUMED", "REAUDIT_REQUESTED"} or kind.endswith(("_RECORDED", "_ANSWERED")):
            if trace.status == "interrupted":
                trace.status = "running"
            if current_span.status == "retried":
                current_span.status = "ok"
        elif kind in {"RUN_COMPLETED", "INGESTION_COMPLETED", "DONE"} or kind.endswith("_CONFIRMED"):
            if current_span.status == "retried":
                current_span.status = "ok"
            trace.status = "ok"
        elif current_span.status == "retried":
            current_span.status = "ok"

    _finalize_span()

    if first_ts is not None and last_ts is not None:
        trace.total_duration_ms = max(0, round((last_ts - first_ts) * 1000))
    else:
        trace.total_duration_ms = sum(span.duration_ms for span in trace.spans)

    by_node: Dict[str, Dict[str, Any]] = {}
    for span in trace.spans:
        bucket = by_node.setdefault(
            span.node,
            {
                "node": span.node,
                "label": span.label,
                "count": 0,
                "total_duration_ms": 0,
                "max_duration_ms": 0,
                "retries": 0,
                "rejections": 0,
            },
        )
        bucket["count"] += 1
        bucket["total_duration_ms"] += span.duration_ms
        if span.duration_ms > bucket["max_duration_ms"]:
            bucket["max_duration_ms"] = span.duration_ms
        bucket["retries"] += span.retries
        bucket["rejections"] += span.rejections
    trace.by_node = by_node
    return trace


def project_trace(
    session: Session,
    run: RunModel,
    *,
    plan: Any = None,
) -> RunTrace:
    """从数据库查询指定 Run 的完整事件流并折叠成 RunTrace 耗时视图。"""
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    return build_trace(events, run_id=run.id, plan=plan)


def render_trace(trace: RunTrace) -> str:
    """渲染为紧凑缩进文本瀑布，便于日志诊断与 CLI 检查。"""
    header = (
        f"run {trace.run_id} [{trace.status}] {trace.total_duration_ms}ms "
        f"({trace.event_count} events, {trace.retry_count} retries, {trace.rejection_count} rejections)"
    )
    lines = [header]
    for span in trace.spans:
        shard_part = f"[{span.shard}]" if span.shard else ""
        label_part = f" ({span.label})" if span.label and span.label != span.node else ""
        retry_part = f" retries={span.retries}" if span.retries else ""
        rej_part = f" rejections={span.rejections}" if span.rejections else ""
        kinds_text = ", ".join(span.kinds)
        lines.append(
            f"  #{span.seq_start}..#{span.seq_end} {span.node}{shard_part}{label_part} "
            f"[{span.status}] {span.duration_ms}ms{retry_part}{rej_part} ({kinds_text})"
        )
    return "\n".join(lines)


# 兼容 re-export：中断协议已独立归位至 app.kernel.interrupt
from .interrupt import (  # noqa: E402, F401
    latest_interrupt,
    make_interrupt_payload,
    matching_answer,
    pending_interrupt_with,
)
