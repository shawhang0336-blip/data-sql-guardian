"""Run：ProdAgent 运行实例上下文、数据通道、预算熔断与生命周期控制面微内核。

职责边界（严格遵守「机制在内，策略在外」，零业务域 import）：
- Channel & RunContext：`ChannelSpec` / `Reducer`（`last_value` / `merge_dict` / `append_list`）/ `Identity` / `RunContext`，
  承载单次 Run 调度的只读状态投影、带归约语义的跨节点共享通道、审计身份与控制守卫；
- BudgetGuard：`BudgetLimits` / `BudgetExceededError` / `BudgetGuard`，
  挂载在 `Bus` 上无侵入统计单次 Run 的 LLM 调用、Token、耗时与步数，并在超限时触发内核熔断；
- Cancellation & Registry：`CancellationToken` / `RunCancelledError` / `RunRegistry`，
  线程安全的活跃 Run 注册表与波次/节点边界受控取消机制；
- Lifecycle Primitives：`create_run` / `fail_run` / `record_run_cancelled` / `cancel_run` /
  `recover_interrupted_runs` / `submit_run` / `delete_run` / `replay_run`，
  统一所有 Run 类型的创世、失败落账、取消、重启恢复、后台推进、钩子删除与哈希链回放。
"""

from contextlib import contextmanager
import contextvars
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.infra.db_tables import EventModel, RunModel
from app.infra.task_runner import TaskRunner, task_runner
from .bus import Bus, global_bus
from .event_log import (
    RunState,
    acquire_run_lock,
    append_event,
    project_many_with,
    project_with,
    verify_chain,
)
from .interrupt import Interrupt, answer_interrupt, record_interrupt_answer  # noqa: F401


# ── 1. 跨节点命名数据通道与运行时上下文（Channel & RunContext） ──────────────────

Reducer = Callable[[Any, Any], Any]


def last_value(_old: Any, new: Any) -> Any:
    """覆盖型 Reducer：取最新写入值。"""
    return new


def merge_dict(old: Any, new: Any) -> Any:
    """字典合并型 Reducer：按 key 合并多波次或多表产出；传入 None 时保留旧值。"""
    if new is None:
        return old
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new
    return {**old, **new}


def append_list(old: Any, new: Any) -> list:
    """列表追加型 Reducer：累积多波次列表产物。"""
    base = list(old) if isinstance(old, list) else []
    if isinstance(new, list):
        return base + new
    return base + [new]


def add_number(old: Any, new: Any) -> Any:
    """数值累加型 Reducer：适用于并发波次汇总计数或耗费指标（满足结合律与交换律）。"""
    return (old or 0) + (new or 0)


_MULTI_WRITER_REDUCERS = (merge_dict, append_list, add_number)


class AmbiguousWriteError(ValueError):
    """同一 BSP 并发波次中，多个任务写入同一个非并发安全通道（如 last_value）时抛出的冲突异常。"""

    def __init__(self, channel: str, writers: Sequence[str]) -> None:
        self.channel = channel
        self.writers = list(writers)
        super().__init__(
            f"并发波次通道写冲突：通道 '{channel}' 不允许同波多写（写入方：{', '.join(self.writers)}），"
            "请改用 merge_dict / append_list / add_number 等归约通道，或显式声明 allow_multi=True。"
        )


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    reducer: Reducer = last_value
    allow_multi: Optional[bool] = None

    @property
    def allows_multi_writer(self) -> bool:
        """是否允许同一 BSP 波次中多个并发任务同时写入本通道。"""
        if self.allow_multi is not None:
            return self.allow_multi
        return self.reducer in _MULTI_WRITER_REDUCERS


@dataclass
class Identity:
    """操作人身份：所有写操作的审计归属。"""

    operator: str = "UNSPECIFIED_OPERATOR"
    approver: Optional[str] = None
    editor: Optional[str] = None

    def as_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"operator": self.operator}
        if self.approver:
            payload["approver"] = self.approver
        if self.editor:
            payload["editor"] = self.editor
        return payload


@dataclass(frozen=True)
class InvocationContext:
    """单次节点尝试的执行标识（严格区分两大方向相反的 ID，线程/并发安全）：

    - `call_id`：每次尝试全局递增且唯一，用于串起调用日志、Bus 事件与 Trace，**绝不能拿来业务去重**；
    - `idempotency_key`：同一逻辑任务在跨重试期间保持恒定，供有副作用的操作登记去重；
    - `has_resume_value` / `resume_value`：中断恢复重跑当前节点时，线程隔离地注入外部喂入的恢复值。
    """

    call_id: str
    idempotency_key: str
    attempt: int = 1
    wave: int = 0
    has_resume_value: bool = False
    resume_value: Any = None


_CURRENT_INVOCATION: contextvars.ContextVar[Optional[InvocationContext]] = contextvars.ContextVar(
    "kernel_current_invocation", default=None
)


@contextmanager
def invocation_scope(invocation: InvocationContext) -> Iterator[InvocationContext]:
    """将当前节点尝试的 `call_id` 与 `idempotency_key` 绑定到当前协程/线程上下文。"""
    token = _CURRENT_INVOCATION.set(invocation)
    try:
        yield invocation
    finally:
        _CURRENT_INVOCATION.reset(token)


@dataclass
class RunContext:
    """单次 Run 调度的运行时容器：承载只读 state 投影、shared 通道、Identity、Bus、取消令牌与预算守卫。"""

    session: Any
    run: Any
    settings: Any
    state: RunState
    identity: Identity = field(default_factory=Identity)
    shared: Dict[str, Any] = field(default_factory=dict)
    channels: Dict[str, ChannelSpec] = field(default_factory=dict)
    bus: Bus = field(default_factory=Bus)
    llm: Any = None
    budget: Optional[Dict[str, Any]] = None
    concurrency: int = 4
    attempt_base: Optional[int] = None
    current_node: str = "created"
    pending_events: list = field(default_factory=list)
    projector: Optional[Callable[[Any, Any], RunState]] = None
    cancellation: Any = None
    budget_guard: Any = None
    resume_values: Dict[str, Any] = field(default_factory=dict)

    def check_cancelled(self) -> None:
        """在节点/波次边界检查取消令牌；若已取消则抛出 RunCancelledError。"""
        if self.cancellation is not None and hasattr(self.cancellation, "raise_if_cancelled"):
            self.cancellation.raise_if_cancelled()

    def check_budget(self) -> None:
        """在节点/波次边界检查成本预算守卫；若超限则抛出 BudgetExceededError。"""
        if self.budget_guard is not None and hasattr(self.budget_guard, "check"):
            self.budget_guard.check(self.bus)
        elif hasattr(self.bus, "check_budget"):
            self.bus.check_budget()

    @property
    def is_cancelled(self) -> bool:
        return bool(self.cancellation is not None and getattr(self.cancellation, "is_cancelled", False))

    @property
    def invocation(self) -> Optional[InvocationContext]:
        """当前线程/并发分片正在执行的尝试标识（并发波次下各 Worker 彼此隔离，绝不串号）。"""
        return _CURRENT_INVOCATION.get()

    @property
    def call_id(self) -> Optional[str]:
        """当前单次尝试的关联 ID（每次重试都会递增变化，用于日志/Trace 关联）。"""
        inv = _CURRENT_INVOCATION.get()
        return inv.call_id if inv is not None else None

    @property
    def idempotency_key(self) -> Optional[str]:
        """当前逻辑任务的稳定业务幂等键（同一任务跨重试保持不变，用于副作用去重）。"""
        inv = _CURRENT_INVOCATION.get()
        return inv.idempotency_key if inv is not None else None

    @property
    def resume_value(self) -> Any:
        """当前中断恢复节点收到的外部喂入值（同波多节点同时恢复时各节点线程隔离，绝不串值）。"""
        inv = _CURRENT_INVOCATION.get()
        if inv is not None and inv.has_resume_value:
            return inv.resume_value
        if "*" in self.resume_values:
            return self.resume_values["*"]
        if self.resume_values:
            return dict(self.resume_values)
        return None

    @resume_value.setter
    def resume_value(self, value: Any) -> None:
        if value is None:
            self.resume_values.clear()
        else:
            self.resume_values["*"] = value

    def put(self, key: str, value: Any) -> None:
        """通过 Channel Reducer 往共享通道写一个命名产物。"""
        spec = self.channels.get(key)
        if spec is not None:
            self.shared[key] = spec.reducer(self.shared.get(key), value)
        else:
            self.shared[key] = value

    def check_wave_writes(self, writes: Sequence[Tuple[str, Dict[str, Any]]]) -> None:
        """原子校验同一 BSP 并发波次的共享通道写集：禁止多个任务同波写非并发安全通道。"""
        if len(writes) <= 1:
            return
        writers_by_key: Dict[str, List[str]] = {}
        for writer, delta in writes:
            if not delta:
                continue
            for key in delta:
                writers_by_key.setdefault(key, []).append(writer)
        for key, writers in writers_by_key.items():
            if len(writers) <= 1:
                continue
            spec = self.channels.get(key)
            if spec is None or not spec.allows_multi_writer:
                raise AmbiguousWriteError(key, writers)

    def apply_wave_writes(self, writes: Sequence[Tuple[str, Dict[str, Any]]]) -> None:
        """先做同波次冲突校验，通过后再按确定性波次顺序通过 Reducer 归并写入。"""
        self.check_wave_writes(writes)
        for _writer, delta in writes:
            if not delta:
                continue
            for key, value in delta.items():
                self.put(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return self.shared.get(key, default)

    def refresh_state(self) -> RunState:
        """从事件账本重新折叠状态（append_event 之后调用）。"""
        if self.projector is not None:
            self.state = self.projector(self.session, self.run)
        else:
            self.state = project_with(self.session, self.run)
        return self.state

    def with_actor(self, **kwargs) -> Identity:
        """入口处设置操作人身份。"""
        for key, value in kwargs.items():
            if hasattr(self.identity, key) and value:
                setattr(self.identity, key, value)
        return self.identity


# ── 2. Run 级横切成本观测与熔断守卫（BudgetGuard） ─────────────────────────────


@dataclass(frozen=True)
class BudgetLimits:
    """运行期成本上限配置；任一项为 None 或 <= 0 表示仅统计不熔断。"""

    max_llm_calls: Optional[int] = None
    max_total_tokens: Optional[int] = None
    max_elapsed_seconds: Optional[float] = None
    max_steps: Optional[int] = None

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "BudgetLimits":
        data = raw or {}

        def _pos_int(key: str) -> Optional[int]:
            val = data.get(key)
            if val is None or val == "":
                return None
            iv = int(val)
            return iv if iv > 0 else None

        def _pos_float(key: str) -> Optional[float]:
            val = data.get(key)
            if val is None or val == "":
                return None
            fv = float(val)
            return fv if fv > 0 else None

        return cls(
            max_llm_calls=_pos_int("max_llm_calls"),
            max_total_tokens=_pos_int("max_total_tokens"),
            max_elapsed_seconds=_pos_float("max_elapsed_seconds"),
            max_steps=_pos_int("max_steps"),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_llm_calls": self.max_llm_calls,
            "max_total_tokens": self.max_total_tokens,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "max_steps": self.max_steps,
        }


class BudgetExceededError(RuntimeError):
    """成本或步数超出 BudgetLimits 时抛出的内核级熔断异常。"""

    def __init__(
        self,
        metric: str,
        used: Any,
        limit: Any,
        snapshot: Dict[str, Any],
        *,
        run_id: Optional[str] = None,
    ) -> None:
        super().__init__(f"执行预算超限（{metric}={used}，上限={limit}），内核已触发熔断。")
        self.metric = metric
        self.used = used
        self.limit = limit
        self.snapshot = snapshot
        self.run_id = run_id


class BudgetGuard:
    """线程安全的横切成本观测与熔断器：挂载到 Run 级 Bus 上自动记账并执行阈值检查。"""

    def __init__(
        self,
        limits: Optional[BudgetLimits] = None,
        *,
        run_id: Optional[str] = None,
        seed: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.limits = limits or BudgetLimits()
        self.run_id = run_id
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        prior = seed if isinstance(seed, dict) else {}
        self._elapsed_offset = max(0.0, float(prior.get("elapsed_seconds") or 0.0))
        self._llm_calls = max(0, int(prior.get("llm_calls") or 0))
        self._prompt_tokens = max(0, int(prior.get("prompt_tokens") or 0))
        self._completion_tokens = max(0, int(prior.get("completion_tokens") or 0))
        self._total_tokens = max(0, int(prior.get("total_tokens") or 0))
        self._total_llm_seconds = max(0.0, float(prior.get("total_llm_seconds") or 0.0))
        self._steps = max(0, int(prior.get("steps") or 0))
        self._tool_rejections = max(0, int(prior.get("tool_rejections") or 0))
        prior_tools = prior.get("by_tool")
        self._by_tool: Dict[str, int] = (
            {str(k): int(v) for k, v in prior_tools.items()}
            if isinstance(prior_tools, dict)
            else {}
        )
        self._in_flight_calls = 0
        self._bus: Optional[Bus] = None

    def attach(self, bus: Bus) -> "BudgetGuard":
        """幂等挂载到指定 Bus：防止重复挂载导致双倍记账；换绑新 Bus 时自动从旧 Bus 卸载。"""
        if self._bus is bus:
            return self
        if self._bus is not None:
            self.detach()
        self._bus = bus
        bus.subscribe("tool.after", self._on_tool_after)
        bus.subscribe("tool.rejected", self._on_tool_rejected)
        bus.subscribe("node.exit", self._on_node_exit)
        if hasattr(bus, "attach_budget"):
            bus.attach_budget(self)
        return self

    def detach(self) -> None:
        """从当前 Bus 退订监听并解除挂载。"""
        bus = self._bus
        if bus is None:
            return
        self._bus = None
        bus.unsubscribe("tool.after", self._on_tool_after)
        bus.unsubscribe("tool.rejected", self._on_tool_rejected)
        bus.unsubscribe("node.exit", self._on_node_exit)
        if getattr(bus, "budget_guard", None) is self and hasattr(bus, "attach_budget"):
            bus.attach_budget(None)

    def _on_tool_after(self, envelope: Dict[str, Any]) -> None:
        tool = str(envelope.get("tool") or "unknown")
        attempts = max(1, int(envelope.get("attempts") or 1))
        elapsed = float(envelope.get("elapsed_seconds") or 0.0)
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        prompt_t = int(usage.get("prompt_tokens") or 0)
        comp_t = int(usage.get("completion_tokens") or 0)
        total_t = int(usage.get("total_tokens") or (prompt_t + comp_t))
        with self._lock:
            self._llm_calls += attempts
            self._prompt_tokens += prompt_t
            self._completion_tokens += comp_t
            self._total_tokens += total_t
            self._total_llm_seconds += elapsed
            self._by_tool[tool] = self._by_tool.get(tool, 0) + attempts

    def _on_tool_rejected(self, _envelope: Dict[str, Any]) -> None:
        with self._lock:
            self._tool_rejections += 1

    def _on_node_exit(self, _envelope: Dict[str, Any]) -> None:
        with self._lock:
            self._steps += 1

    def _snapshot_locked(self) -> Dict[str, Any]:
        elapsed = round(self._elapsed_offset + (time.monotonic() - self._started_at), 2)
        return {
            "llm_calls": self._llm_calls,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._total_tokens,
            "total_llm_seconds": round(self._total_llm_seconds, 2),
            "elapsed_seconds": elapsed,
            "steps": self._steps,
            "tool_rejections": self._tool_rejections,
            "by_tool": dict(self._by_tool),
            "limits": self.limits.as_dict(),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _find_breach(self, snap: Dict[str, Any]) -> Optional[Tuple[str, Any, Any]]:
        if self.limits.max_llm_calls is not None and snap["llm_calls"] >= self.limits.max_llm_calls:
            return ("max_llm_calls", snap["llm_calls"], self.limits.max_llm_calls)
        if self.limits.max_total_tokens is not None and snap["total_tokens"] >= self.limits.max_total_tokens:
            return ("max_total_tokens", snap["total_tokens"], self.limits.max_total_tokens)
        if self.limits.max_elapsed_seconds is not None and snap["elapsed_seconds"] >= self.limits.max_elapsed_seconds:
            return ("max_elapsed_seconds", snap["elapsed_seconds"], self.limits.max_elapsed_seconds)
        if self.limits.max_steps is not None and snap["steps"] >= self.limits.max_steps:
            return ("max_steps", snap["steps"], self.limits.max_steps)
        return None

    def _raise_breach(
        self,
        breach: Tuple[str, Any, Any],
        snap: Dict[str, Any],
        bus: Optional[Bus],
    ) -> None:
        metric, used, limit = breach
        active_bus = bus or self._bus
        if active_bus is not None and hasattr(active_bus, "fire"):
            active_bus.fire(
                "budget.exceeded",
                {
                    "run_id": self.run_id,
                    "metric": metric,
                    "used": used,
                    "limit": limit,
                    "snapshot": snap,
                },
            )
        raise BudgetExceededError(metric, used, limit, snap, run_id=self.run_id)

    def begin_call(self, bus: Optional[Bus] = None) -> None:
        """在锁内原子校验预算并预占一次在途 LLM 调用名额，防止并发突破上限。"""
        breach = None
        snap: Dict[str, Any] = {}
        with self._lock:
            snap = self._snapshot_locked()
            projected_calls = self._llm_calls + self._in_flight_calls
            projected_snap = {**snap, "llm_calls": max(snap["llm_calls"], projected_calls)}
            breach = self._find_breach(projected_snap)
            if breach is None:
                self._in_flight_calls += 1
                return
            snap = projected_snap
        self._raise_breach(breach, snap, bus)

    def end_calls(self, count: int = 1) -> None:
        """归还已完成或中止的在途 LLM 调用预占计数。"""
        with self._lock:
            self._in_flight_calls = max(0, self._in_flight_calls - max(0, int(count)))

    def check(self, bus: Optional[Bus] = None) -> None:
        """检查是否触达任一预算上限；超限时广播 budget.exceeded 并抛出 BudgetExceededError。"""
        snap = self.snapshot()
        breach = self._find_breach(snap)
        if breach is not None:
            self._raise_breach(breach, snap, bus)


# ── 3. 运行取消令牌与活跃实例注册表（Cancellation & Registry） ───────────────────


class RunCancelledError(Exception):
    """Run 在节点/波次边界收到取消信号后抛出的受控中止异常。"""

    def __init__(self, reason: str = "Run cancelled by operator") -> None:
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """线程安全的 Run 级取消令牌，供 Scheduler 与执行节点轮询或检查。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str = ""

    def cancel(self, reason: str = "Run cancelled by operator") -> None:
        with self._lock:
            if not self._reason:
                self._reason = reason or "Run cancelled by operator"
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason or "Run cancelled by operator"

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RunCancelledError(self.reason)

    def wait(self, timeout: float) -> bool:
        """等待指定秒数或直到收到取消信号；若因取消提前唤醒返回 True。"""
        return self._event.wait(max(0.0, float(timeout)))


class RunRegistry:
    """进程内活跃 Run 注册表：追踪正在执行的 Run 与其取消令牌。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: Dict[str, CancellationToken] = {}

    def register(self, run_id: str, token: Optional[CancellationToken] = None) -> CancellationToken:
        active_token = token or CancellationToken()
        with self._lock:
            self._tokens[run_id] = active_token
        return active_token

    def unregister(self, run_id: str, token: Optional[CancellationToken] = None) -> None:
        with self._lock:
            current = self._tokens.get(run_id)
            if current is None:
                return
            if token is None or current is token:
                self._tokens.pop(run_id, None)

    def get_token(self, run_id: str) -> Optional[CancellationToken]:
        with self._lock:
            return self._tokens.get(run_id)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._tokens

    def active_run_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._tokens.keys())

    def request_cancel(self, run_id: str, reason: str = "Run cancelled by operator") -> bool:
        with self._lock:
            token = self._tokens.get(run_id)
        if token is None:
            return False
        token.cancel(reason)
        return True


_GLOBAL_REGISTRY = RunRegistry()


def run_registry() -> RunRegistry:
    """返回进程全局共享的活跃 Run 注册表单例。"""
    return _GLOBAL_REGISTRY


def create_run(
    session: Session,
    spec_id: str,
    initial_node: str = "created",
    initial_payload: Optional[Dict[str, Any]] = None,
    *,
    run_id: Optional[str] = None,
    bus: Any = None,
) -> RunModel:
    """创建 RunModel 身份并追加创世事件 RUN_CREATED（确保每个 Run 都有合法账本起点）。"""
    run = RunModel(id=run_id or str(uuid.uuid4()), spec_id=spec_id)
    session.add(run)
    session.flush()
    payload: Dict[str, Any] = {"spec_id": spec_id}
    if initial_payload:
        payload.update(initial_payload)
    append_event(session, run, "RUN_CREATED", initial_node, payload, bus=bus)
    return run


def fail_run(
    session: Session,
    run: RunModel,
    node: str,
    error: str,
    *,
    error_details: Optional[Dict[str, Any]] = None,
    resume_from_node: Optional[str] = None,
    resume_when: Optional[str] = None,
    extra_result: Optional[Dict[str, Any]] = None,
    bus: Any = None,
    commit: bool = True,
) -> EventModel:
    """统一失败落账原语：排空 Bus 拦截 → 写 RUN_FAILED 并提交真相 → 再广播 node.error & run.failed。"""
    active_bus = bus if bus is not None else global_bus()
    if hasattr(active_bus, "drain_rejections"):
        for rej in active_bus.drain_rejections():
            append_event(session, run, "BUS_REJECTED", node, rej, bus=active_bus)

    result_payload: Dict[str, Any] = {"error": error}
    if error_details is not None:
        result_payload["error_details"] = error_details
    if extra_result:
        result_payload.update(extra_result)
    event_payload: Dict[str, Any] = {"node": node, "result": result_payload}
    if resume_from_node is not None:
        event_payload["resume_from_node"] = resume_from_node
    if resume_when is not None:
        event_payload["resume_when"] = resume_when

    event = append_event(session, run, "RUN_FAILED", node, event_payload, bus=active_bus)
    if commit:
        session.commit()
    if hasattr(active_bus, "fire"):
        active_bus.fire("node.error", {"run_id": run.id, "node": node, "error": error})
        active_bus.fire(
            "run.failed",
            {"run_id": run.id, "node": node, "status": "FAILED", "error": error},
        )
    return event


def record_run_cancelled(
    session: Session,
    run: RunModel,
    node: str,
    reason: str = "Run cancelled by operator",
    *,
    resume_from_node: Optional[str] = None,
    resume_when: Optional[str] = None,
    extra_result: Optional[Dict[str, Any]] = None,
    bus: Any = None,
    commit: bool = True,
) -> EventModel:
    """落账受控取消事件 RUN_CANCELLED 并提交真相，再广播 run.cancelled（消除 SSE 与 DB 竞态）。"""
    active_bus = bus if bus is not None else global_bus()
    result_payload: Dict[str, Any] = {"error": reason, "cancelled": True}
    if extra_result:
        result_payload.update(extra_result)
    event_payload: Dict[str, Any] = {
        "node": node,
        "reason": reason,
        "result": result_payload,
    }
    if resume_from_node is not None:
        event_payload["resume_from_node"] = resume_from_node
    if resume_when is not None:
        event_payload["resume_when"] = resume_when
    event = append_event(session, run, "RUN_CANCELLED", node, event_payload, bus=active_bus)
    if commit:
        session.commit()
    if hasattr(active_bus, "fire"):
        active_bus.fire(
            "run.cancelled",
            {"run_id": run.id, "node": node, "status": "FAILED", "reason": reason},
        )
    return event


def cancel_run(
    session: Session,
    run: RunModel,
    *,
    active_statuses: Iterable[str],
    node: str = "cancel",
    reason: str = "Run cancelled by operator",
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    bus: Any = None,
    registry: Optional[RunRegistry] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """取消指定运行中 Run：若后台线程活跃则投递取消令牌，否则直接落账 RUN_CANCELLED。"""
    run_id = run.id
    acquire_run_lock(session, run_id)
    fresh_run = session.get(RunModel, run_id)
    if fresh_run is None:
        raise ValueError("Run not found")
    run = fresh_run
    project_fn = projector or project_with
    reg = registry if registry is not None else run_registry()
    state = project_fn(session, run)
    allowed = set(active_statuses)
    if state.status not in allowed and not reg.is_active(run.id):
        raise ValueError(f"Run is not active (status={state.status})")

    signalled = reg.request_cancel(run.id, reason)
    if signalled:
        return {
            "run_id": run.id,
            "signalled": True,
            "cancelled": False,
            "status": state.status,
            "reason": reason,
        }

    record_run_cancelled(
        session,
        run,
        node=node,
        reason=reason,
        resume_from_node=state.resume_from_node,
        resume_when=state.resume_when,
        bus=bus,
        commit=commit,
    )
    new_state = project_fn(session, run)
    return {
        "run_id": run.id,
        "signalled": False,
        "cancelled": True,
        "status": new_state.status,
        "reason": reason,
    }


def recover_interrupted_runs(
    session: Session,
    active_statuses: Iterable[str],
    *,
    runs: Optional[Iterable[RunModel]] = None,
    projector_many: Optional[Callable[[Session, Iterable[RunModel]], Dict[str, RunState]]] = None,
    reason: str = "Run interrupted by process restart",
    bus: Any = None,
    commit: bool = True,
) -> List[str]:
    """进程重启中断恢复原语：将遗留在运行态的批次统一落账为失败，等待人工断点续跑。"""
    target_runs = list(runs) if runs is not None else session.query(RunModel).all()
    if not target_runs:
        return []
    project_fn = projector_many or project_many_with
    states = project_fn(session, target_runs)
    active = set(active_statuses)
    recovered: List[str] = []
    for run in target_runs:
        state = states[run.id]
        if state.status not in active:
            continue
        fail_run(
            session,
            run,
            node="startup",
            error=reason,
            resume_from_node=state.resume_from_node,
            resume_when=state.resume_when,
            bus=bus,
            commit=False,
        )
        recovered.append(run.id)
    if recovered and commit:
        session.commit()
    return recovered


def submit_run(
    run_id: str,
    session_factory: Callable[[], Session],
    driver_fn: Callable[[Session, RunModel, CancellationToken], Any],
    *,
    allowed_statuses: Iterable[str] = ("RUNNING", "READY_TO_AUDIT"),
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    error_details_fn: Optional[Callable[[BaseException, str], Dict[str, Any]]] = None,
    runner: Optional[TaskRunner] = None,
    registry: Optional[RunRegistry] = None,
) -> bool:
    """统一后台提交入口：按 `run:<id>` 防重入 + 注册取消令牌 + 异常/取消统一落账。"""
    project_fn = projector or project_with
    active_runner = runner if runner is not None else task_runner()
    active_registry = registry if registry is not None else run_registry()
    allowed = set(allowed_statuses)

    def _worker() -> None:
        token = active_registry.register(run_id)
        try:
            with session_factory() as session:
                run = session.get(RunModel, run_id)
                if not run:
                    return
                if project_fn(session, run).status not in allowed:
                    return
                driver_fn(session, run, token)
        except RunCancelledError as exc:
            with session_factory() as session:
                run = session.get(RunModel, run_id)
                if run and project_fn(session, run).status in allowed:
                    st = project_fn(session, run)
                    record_run_cancelled(
                        session,
                        run,
                        node=st.current_node or "background_executor",
                        reason=exc.reason,
                        resume_from_node=st.resume_from_node,
                        resume_when=st.resume_when,
                    )
        except Exception as exc:  # noqa: BLE001
            with session_factory() as session:
                run = session.get(RunModel, run_id)
                if run and project_fn(session, run).status in allowed:
                    st = project_fn(session, run)
                    details = error_details_fn(exc, "background_executor") if error_details_fn else None
                    fail_run(
                        session,
                        run,
                        node="background_executor",
                        error=f"{type(exc).__name__}: {exc}",
                        error_details=details,
                        resume_from_node=st.resume_from_node,
                        resume_when=st.resume_when,
                    )
        finally:
            active_registry.unregister(run_id, token)

    return active_runner.submit(f"run:{run_id}", _worker)


def delete_run(
    session: Session,
    run: RunModel,
    *,
    active_statuses: Iterable[str],
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    pre_delete_hooks: Optional[Iterable[Callable[[Session, RunModel, Dict[str, Any]], None]]] = None,
    post_commit_hooks: Optional[Iterable[Callable[[Session, Dict[str, Any]], None]]] = None,
    registry: Optional[RunRegistry] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """统一 Run 删除原语：运行态守卫 → 业务前置清理钩子 → 删事件与身份 → 提交 → 外部资源清理钩子。"""
    run_id = run.id
    acquire_run_lock(session, run_id)
    fresh_run = session.get(RunModel, run_id)
    if fresh_run is None:
        raise ValueError("Run not found")
    run = fresh_run
    project_fn = projector or project_with
    reg = registry if registry is not None else run_registry()
    if project_fn(session, run).status in set(active_statuses) or reg.is_active(run.id):
        raise ValueError("Running validation batches cannot be deleted")

    context: Dict[str, Any] = {"run_id": run.id}
    for hook in pre_delete_hooks or ():
        hook(session, run, context)

    session.query(EventModel).filter(EventModel.run_id == run_id).delete(synchronize_session=False)
    deleted = session.query(RunModel).filter(RunModel.id == run_id).delete(synchronize_session=False)
    if fresh_run in session:
        session.expunge(fresh_run)
    if run in session:
        session.expunge(run)
    if deleted == 0:
        raise ValueError("Run not found")
    session.flush()
    if commit:
        session.commit()

    for hook in post_commit_hooks or ():
        hook(session, context)
    return context


def replay_run(
    session: Session,
    run: RunModel,
    *,
    projector: Optional[Callable[[Session, RunModel], RunState]] = None,
    registry: Optional[RunRegistry] = None,
) -> Dict[str, Any]:
    """统一回放与哈希链完整性校验原语。"""
    project_fn = projector or project_with
    reg = registry if registry is not None else run_registry()
    state = project_fn(session, run)
    return {
        "status": state.status,
        "current_node": state.current_node,
        "attempt": state.attempt,
        "result": state.result_json,
        "chain_valid": verify_chain(session, run),
        "active": reg.is_active(run.id),
    }
