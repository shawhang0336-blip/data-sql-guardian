"""Scheduler：ProdAgent 真 BSP 波次执行引擎微内核（Wave + Send + Goto + Interrupt + Retry/Timeout）。

职责边界：
- 不认识任何具体业务阶段——只读 Plan、算就绪波次（Ready Set）、并发跑 Wave、在屏障统一校验并归并落账；
- 完整支持 ProdAgent 核心控制与弹性原语：
  1. `Send(node, input)`：运行时动态扇出（同波次并发执行，屏障先做 `AmbiguousWriteError` 冲突检测再按确定性顺序归约）；
  2. `goto`：运行时跳转/自愈回边（落 `GO_REDIRECT` 后重入目标节点）；
  3. `Interrupt`：一等人工中断指令（屏障落账中断事件、广播 `run.interrupted` 并立刻挂起）；
  4. 节点级声明式 `timeout` 与 `RetryPolicy`（指数退避、广播 `node.retry`、屏障落账 `NODE_RETRY`、支持取消唤醒）；
- 同步提供可复用的内核级波次执行原语 `execute_wave`，统一托管 `wave.fanout` / `wave.barrier`、
  并发池、取消检查（`CancellationToken`）与预算熔断检查（`BudgetGuard`）。
"""

import contextvars
import threading
import time
from concurrent.futures import (
    CancelledError as FuturesCancelledError,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from .bus import global_bus
from .event_log import append_event
from .interrupt import Interrupt
from .plan import WorkflowPlan
from .run import (
    AmbiguousWriteError,
    BudgetExceededError,
    InvocationContext,
    RunCancelledError,
    RunContext,
    invocation_scope,
)

T = TypeVar("T")
R = TypeVar("R")

# Node body 的统一契约：body(input, ctx) -> Outcome
NodeBody = Callable[[Any, RunContext], "Outcome"]

_NON_RETRYABLE_ERRORS = (
    RunCancelledError,
    BudgetExceededError,
    AmbiguousWriteError,
    KeyboardInterrupt,
    SystemExit,
)


class NodeTimeoutError(TimeoutError):
    """节点执行超出 `PlanNode.timeout` 声明上限时抛出的内核超时异常。"""

    def __init__(self, node: str, timeout: float) -> None:
        super().__init__(f"节点 {node} 执行超时（上限 {timeout}s）")
        self.node = node
        self.timeout = timeout


class WaveExecutionError(RuntimeError):
    """同一并发波次内有多个任务同时失败时抛出的汇总异常，完整保留全部 `(label, error)` 而不吞错。"""

    def __init__(self, errors: Sequence[Tuple[str, BaseException]]) -> None:
        self.errors: List[Tuple[str, BaseException]] = list(errors)
        self.failed_labels: List[str] = [label for label, _ in self.errors]
        self.first_error: BaseException = self.errors[0][1]
        summary = "；".join(f"[{label}] {type(err).__name__}: {err}" for label, err in self.errors)
        super().__init__(
            f"并发波次出现 {len(self.errors)} 项失败（{', '.join(self.failed_labels)}）：{summary}"
        )


class GraphStalledError(RuntimeError):
    """静态工作流图卡死（非终态/非门控节点算不出任何匹配出边）时抛出的内核异常。"""

    def __init__(self, stalled_nodes: Sequence[str], when_tags: Dict[str, Optional[str]]) -> None:
        self.stalled_nodes = list(stalled_nodes)
        self.when_tags = dict(when_tags)
        detail = "，".join(f"{node}(when={self.when_tags.get(node)!r})" for node in self.stalled_nodes)
        super().__init__(f"工作流图卡死，以下阶段节点没有可匹配的出边：{detail}")


class MaxWavesExceededError(RuntimeError):
    """波次循环达到 `max_steps` 安全阀仍未进入终态或人工门时抛出的防空转熔断异常。"""

    def __init__(self, max_steps: int, cursor: str, pending_nodes: Sequence[str]) -> None:
        self.max_steps = max_steps
        self.cursor = cursor
        self.pending_nodes = list(pending_nodes)
        super().__init__(
            f"工作流调度超过最大波次上限（max_steps={max_steps}，当前游标={cursor}，"
            f"待执行节点={self.pending_nodes}），内核已熔断防止死循环空转。"
        )


@dataclass(frozen=True)
class Send:
    """动态扇出指令：向目标节点派发一条带独立 input 的并发任务，在下一 BSP 波次并行执行。"""

    node: str
    input: Any = None
    label: Optional[str] = None


@dataclass
class Outcome:
    """节点执行结果。只声明事实与控制指令，不直接改 Run 或调下游。"""

    output: Any = None
    shared: Dict[str, Any] = field(default_factory=dict)  # 写入 Channel 的产物
    events: List[tuple] = field(default_factory=list)  # [(kind, node, payload), ...]
    goto: Optional[str] = None  # 运行时跳转目标（必须为 Plan 中的合法节点）
    sends: List[Send] = field(default_factory=list)  # 动态扇出任务列表（下一 BSP 波次并发执行）
    control: Dict[str, Any] = field(default_factory=dict)  # goto 附带的控制信息
    when: str = "always"  # Plan 条件出边标签
    interrupt: Optional[Interrupt] = None  # 一等人工中断指令（存在时屏障落账后立即挂起）

    def add_event(self, kind: str, node: str, payload: Dict[str, Any]) -> "Outcome":
        self.events.append((kind, node, payload))
        return self

    @classmethod
    def park(
        cls,
        kind: str,
        question: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        node: str = "",
        resume_node: Optional[str] = None,
        identity: Optional[Dict[str, Any]] = None,
        events: Optional[Sequence[tuple]] = None,
        shared: Optional[Dict[str, Any]] = None,
        when: str = "always",
    ) -> "Outcome":
        """快捷构造人工/外部中断挂起结果：`node` 留空时由 Scheduler 在屏障处自动补齐。"""
        return cls(
            shared=dict(shared or {}),
            events=list(events or ()),
            when=when,
            interrupt=Interrupt(
                kind=kind,
                node=node,
                question=question,
                pending_input=dict(payload or {}),
                resume_node=resume_node,
                identity=identity,
            ),
        )


@dataclass
class StepResult:
    node: str
    outcome: Outcome
    input: Any = None
    label: Optional[str] = None
    attempts: int = 1
    duration_ms: int = 0
    call_id: Optional[str] = None
    idempotency_key: Optional[str] = None


def _invoke_body_with_timeout(
    body: NodeBody,
    task_input: Any,
    ctx: RunContext,
    timeout: Optional[float],
    node_name: str,
) -> Outcome:
    if timeout is None:
        return body(task_input, ctx)
    captured = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(captured.run, body, task_input, ctx)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise NodeTimeoutError(node_name, timeout) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _sleep_cancelable(delay: float, cancellation: Any) -> None:
    if delay <= 0:
        return
    if cancellation is not None and hasattr(cancellation, "wait"):
        if cancellation.wait(delay):
            cancellation.raise_if_cancelled()
        return
    time.sleep(delay)


def execute_wave(
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    bus: Any = None,
    run_id: Optional[str] = None,
    stage: Optional[str] = None,
    labels: Optional[Sequence[str]] = None,
    max_workers: int = 4,
    cancellation: Any = None,
    budget_guard: Any = None,
    partial_results_out: Optional[List[R]] = None,
) -> List[R]:
    """内核级 BSP 波次执行原语：波前门禁 → `wave.fanout` → 并发执行 → `wave.barrier`（保序返回）。

    若传入 `partial_results_out` 且并发波次中部分子任务失败，会先把已成功的结果按确定性顺序回填，
    供 Scheduler 在主线程屏障先行持久化已完成分片的检查点，再向上抛出首个异常。
    """
    if not items:
        return []
    if cancellation is not None and hasattr(cancellation, "raise_if_cancelled"):
        cancellation.raise_if_cancelled()
    active_bus = bus if bus is not None else global_bus()
    if budget_guard is not None and hasattr(budget_guard, "check"):
        budget_guard.check(active_bus)
    elif hasattr(active_bus, "check_budget"):
        active_bus.check_budget()

    item_labels = list(labels) if labels is not None else [str(i) for i in range(len(items))]
    fanout_meta: Dict[str, Any] = {"wave_size": len(items), "labels": item_labels}
    if run_id:
        fanout_meta["run_id"] = run_id
    if stage:
        fanout_meta["stage"] = stage
        fanout_meta["table_codes"] = item_labels
    if hasattr(active_bus, "fire"):
        active_bus.fire("wave.fanout", fanout_meta)

    def _guarded_worker(item: T) -> R:
        if cancellation is not None and hasattr(cancellation, "raise_if_cancelled"):
            cancellation.raise_if_cancelled()
        return worker(item)

    worker_count = max(1, min(int(max_workers or 1), len(items)))
    if worker_count == 1 or len(items) == 1:
        results: List[R] = []
        for item in items:
            try:
                results.append(_guarded_worker(item))
            except BaseException:
                if partial_results_out is not None and results:
                    partial_results_out.extend(results)
                raise
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_guarded_worker, item) for item in items]
            ordered: List[R] = []
            failures: List[Tuple[str, BaseException]] = []
            priority_exc: Optional[BaseException] = None
            for idx, future in enumerate(futures):
                label = item_labels[idx] if idx < len(item_labels) else str(idx)
                try:
                    ordered.append(future.result())
                    if cancellation is not None and getattr(cancellation, "is_cancelled", False):
                        for pending in futures[idx + 1 :]:
                            pending.cancel()
                except FuturesCancelledError:
                    # 队列中被取消的后续任务，不视为新的业务失败
                    continue
                except BaseException as exc:
                    if isinstance(exc, (RunCancelledError, BudgetExceededError)):
                        if priority_exc is None:
                            priority_exc = exc
                            for pending in futures[idx + 1 :]:
                                pending.cancel()
                    else:
                        failures.append((label, exc))
            if priority_exc is not None or failures:
                if partial_results_out is not None and ordered:
                    partial_results_out.extend(ordered)
                if priority_exc is not None:
                    raise priority_exc
                if len(failures) == 1:
                    raise failures[0][1]
                raise WaveExecutionError(failures)
            results = ordered

    barrier_meta: Dict[str, Any] = {"merged_count": len(results), "labels": item_labels}
    if run_id:
        barrier_meta["run_id"] = run_id
    if stage:
        barrier_meta["stage"] = stage
        barrier_meta["table_codes"] = item_labels
    if hasattr(active_bus, "fire"):
        active_bus.fire("wave.barrier", barrier_meta)
    return results


class Scheduler:
    """按 Plan 与 BSP 超步模型驱动一次执行，直到人工中断门或终态。"""

    def __init__(self, plan: WorkflowPlan, bodies: Dict[str, NodeBody]) -> None:
        self.plan = plan
        self.bodies = bodies
        self._node_names = {node.name for node in plan.nodes}
        self._call_seq = 0
        self._call_lock = threading.Lock()
        unknown = set(bodies) - self._node_names
        if unknown:
            raise ValueError(f"Node body 不在 Plan 中：{sorted(unknown)}")

    def _next_call_id(self, run_id: str) -> str:
        with self._call_lock:
            self._call_seq += 1
            return f"{run_id}:c{self._call_seq}"

    def _next_ready(self, node: str, ctx: RunContext, when: Optional[str] = None) -> List[str]:
        """从当前游标 + 条件标签计算下一波次的所有就绪节点（支持单后继与静态多边扇出）。"""
        if node not in self._node_names:
            return []
        plan_node = self.plan.node(node)
        if plan_node.kind == "terminal":
            return []
        if plan_node.kind == "gate" and ctx.state.status.startswith("WAITING_"):
            return []

        edges = self.plan.successors(node)
        for tag in (when, ctx.state.when, "always"):
            if not tag:
                continue
            if tag == "always":
                matched = [e.dst for e in edges if e.when == "always"]
            else:
                matched = [e.dst for e in edges if e.when == tag]
            if matched:
                # 保持声明顺序去重
                return list(dict.fromkeys(matched))
        return []

    def _next(self, node: str, ctx: RunContext, when: Optional[str] = None) -> Optional[str]:
        """兼容单后继查询。"""
        ready = self._next_ready(node, ctx, when=when)
        return ready[0] if ready else None

    def _run_task(
        self,
        task: Send,
        ctx: RunContext,
        wave_index: int = 0,
        *,
        has_resume_value: bool = False,
        resume_value: Any = None,
    ) -> Tuple[Send, Outcome, List[tuple], int, int, str, str]:
        body = self.bodies[task.node]
        plan_node = self.plan.node(task.node)
        policy = plan_node.retry
        max_attempts = policy.max_attempts if policy is not None else 1
        idempotency_key = f"{ctx.run.id}:w{wave_index}:{task.node}:{task.label or '_'}"
        started = time.monotonic()

        retry_events: List[tuple] = []
        attempt = 0
        while True:
            attempt += 1
            call_id = self._next_call_id(ctx.run.id)
            if attempt == 1 and hasattr(ctx.bus, "fire"):
                enter_meta: Dict[str, Any] = {
                    "run_id": ctx.run.id,
                    "node": task.node,
                    "attempt": ctx.state.attempt,
                    "call_id": call_id,
                    "idempotency_key": idempotency_key,
                }
                if task.label:
                    enter_meta["label"] = task.label
                ctx.bus.fire("node.enter", enter_meta)
            ctx.check_cancelled()
            inv = InvocationContext(
                call_id=call_id,
                idempotency_key=idempotency_key,
                attempt=attempt,
                wave=wave_index,
                has_resume_value=has_resume_value,
                resume_value=resume_value,
            )
            try:
                with invocation_scope(inv):
                    outcome = _invoke_body_with_timeout(body, task.input, ctx, plan_node.timeout, task.node)
                duration_ms = max(0, round((time.monotonic() - started) * 1000))
                return task, outcome, retry_events, attempt, duration_ms, call_id, idempotency_key
            except BaseException as exc:
                if (
                    isinstance(exc, _NON_RETRYABLE_ERRORS)
                    or policy is None
                    or attempt >= max_attempts
                    or not policy.matches(exc)
                ):
                    raise
                delay = policy.delay_for(attempt - 1)
                retry_payload: Dict[str, Any] = {
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "delay_seconds": round(delay, 4),
                    "call_id": call_id,
                    "idempotency_key": idempotency_key,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if task.label:
                    retry_payload["label"] = task.label
                retry_events.append(("NODE_RETRY", task.node, retry_payload))
                if hasattr(ctx.bus, "fire"):
                    ctx.bus.fire(
                        "node.retry",
                        {"run_id": ctx.run.id, "node": task.node, **retry_payload},
                    )
                ctx.check_cancelled()
                ctx.check_budget()
                _sleep_cancelable(delay, ctx.cancellation)

    def _build_static_wave(
        self,
        cursor: str,
        ctx: RunContext,
        last_when: Optional[str],
        initial_input: Any,
        last_wave_nodes: List[str],
        last_wave_whens: Dict[str, str],
        last_wave_outputs: List[Tuple[str, Any]],
        last_wave_was_send: bool,
    ) -> List[Send]:
        if not last_wave_nodes:
            ready_nodes = self._next_ready(cursor, ctx, when=last_when)
            return [Send(name, initial_input) for name in ready_nodes]
        preds_by_dst: Dict[str, List[str]] = {}
        for src_node in dict.fromkeys(last_wave_nodes):
            for dst in self._next_ready(src_node, ctx, when=last_wave_whens.get(src_node)):
                bucket = preds_by_dst.setdefault(dst, [])
                if src_node not in bucket:
                    bucket.append(src_node)
        output_by_node: Dict[str, Any] = {}
        for src_node, out_val in last_wave_outputs:
            output_by_node[src_node] = out_val
        next_tasks: List[Send] = []
        for dst, preds in preds_by_dst.items():
            if last_wave_was_send and len(preds) == 1:
                edge_input: Any = [
                    out_val for src_node, out_val in last_wave_outputs if src_node == preds[0]
                ]
            elif len(preds) > 1:
                edge_input = {p: output_by_node.get(p) for p in preds}
            else:
                edge_input = output_by_node.get(preds[0])
            next_tasks.append(Send(dst, edge_input))
        return next_tasks

    def resume(
        self,
        ctx: RunContext,
        resume_value: Any = None,
        *,
        resume_nodes: Optional[Sequence[str]] = None,
        max_steps: int = 64,
        max_workers: Optional[int] = None,
        record_event: bool = True,
        answer_kind: str = "RUN_RESUMED",
        answer_payload: Optional[Dict[str, Any]] = None,
    ) -> List[StepResult]:
        """从人工/外部中断恢复执行：可选落账 `RUN_RESUMED` 事实，按节点喂入 `ctx.resume_value` 并驱动续跑。

        同时支持两种 HITL 范式：
        1. **单节点/同波多节点 `Outcome.park` 喂值重跑**：把挂起节点重装入首波，并线程隔离地注入 `ctx.resume_value`
           （单节点传裸值，多节点传 `{node_name: value}` 字典）；
        2. **显式 Gate 节点分离推进**（如 RegOps `human_gate`）：若挂起门节点无 body，则按 `Plan` 后继边继续推进。
        """
        inferred_dict_nodes = False
        if resume_nodes is not None:
            targets = list(resume_nodes)
        elif (
            isinstance(resume_value, dict)
            and resume_value
            and all(isinstance(k, str) and k in self.bodies for k in resume_value)
        ):
            targets = list(resume_value.keys())
            inferred_dict_nodes = True
        else:
            candidate = ctx.state.pending_target or ctx.state.resume_from_node or ctx.state.current_node
            targets = [candidate] if candidate in self.bodies else []

        resume_map: Dict[str, Any] = {}
        if len(targets) > 1:
            if isinstance(resume_value, dict):
                resume_map = {name: resume_value.get(name) for name in targets}
            else:
                resume_map = {name: resume_value for name in targets}
        elif len(targets) == 1:
            only = targets[0]
            if inferred_dict_nodes:
                resume_map = {only: resume_value[only]}
            else:
                resume_map = {only: resume_value}

        if record_event:
            event_node = targets[0] if targets else (ctx.state.current_node or self.plan.entry)
            payload: Dict[str, Any] = {
                "resume_nodes": targets,
                "has_resume_value": resume_value is not None,
            }
            if answer_payload:
                payload.update(answer_payload)
            append_event(ctx.session, ctx.run, answer_kind, event_node, payload, bus=ctx.bus)
            ctx.session.flush()
            ctx.refresh_state()

        initial_wave = [Send(name, None) for name in targets] if targets else None
        return self.drive(
            ctx,
            max_steps=max_steps,
            max_workers=max_workers,
            initial_wave=initial_wave,
            resume_values=resume_map or None,
        )

    def drive(
        self,
        ctx: RunContext,
        max_steps: int = 64,
        max_workers: Optional[int] = None,
        initial_input: Any = None,
        *,
        initial_wave: Optional[Sequence[Send]] = None,
        resume_values: Optional[Dict[str, Any]] = None,
    ) -> List[StepResult]:
        """按 BSP 超步波次执行到人工中断门或终态，返回全部执行轨迹。

        自动沿边向下一波节点喂入 `input`：
        - 入口首波：传入 `initial_input`；
        - `Send` 动态副本：传入各自 `Send.input`；
        - `Goto` 跳转目标：传入 `Outcome.control`（若为空则取 `Outcome.output`）；
        - 单前驱静态边：直接传入前驱 `Outcome.output`；
        - 多前驱静态合流：传入 `{前驱节点名: output}` 字典；
        - 模板（`Send` 波次）前驱：传入该模板所有并发实例 `output` 组成的有序列表。
        """
        if max_steps < 1:
            raise ValueError("max_steps 必须 >= 1")
        executed: List[StepResult] = []
        cursor = ctx.state.current_node
        if initial_wave is not None:
            pending_wave: Optional[List[Send]] = list(initial_wave)
        elif ctx.state.pending_target:
            pending_wave = [Send(ctx.state.pending_target, initial_input)]
        else:
            pending_wave = None
        wave1_resume_values: Dict[str, Any] = dict(resume_values or {})
        last_when: Optional[str] = None
        is_send_wave = False
        last_wave_nodes: List[str] = []
        last_wave_whens: Dict[str, str] = {}
        last_wave_outputs: List[Tuple[str, Any]] = []
        last_wave_was_send = False
        wave_width = max(1, int(max_workers or ctx.concurrency or 4))

        for wave_index in range(1, max_steps + 1):
            ctx.check_cancelled()
            ctx.check_budget()

            if pending_wave is None:
                pending_wave = self._build_static_wave(
                    cursor,
                    ctx,
                    last_when,
                    initial_input,
                    last_wave_nodes,
                    last_wave_whens,
                    last_wave_outputs,
                    last_wave_was_send,
                )
                is_send_wave = False
            if not pending_wave:
                if (
                    ctx.state.status not in {"COMPLETED", "FAILED"}
                    and not ctx.state.status.startswith("WAITING_")
                ):
                    active_sources = list(dict.fromkeys(last_wave_nodes)) if last_wave_nodes else [cursor]
                    stalled = [
                        name
                        for name in active_sources
                        if name in self._node_names and self.plan.node(name).kind == "stage"
                    ]
                    if stalled:
                        when_map = {
                            name: (last_wave_whens.get(name) if last_wave_nodes else (last_when or ctx.state.when))
                            for name in stalled
                        }
                        raise GraphStalledError(stalled, when_map)
                break
            wave = [task for task in pending_wave if task.node in self.bodies]
            current_is_send_wave = is_send_wave
            pending_wave = None
            is_send_wave = False
            last_when = None
            if not wave:
                break

            active_resume = wave1_resume_values if wave_index == 1 else {}

            def _exec_task(task: Send) -> Tuple[Send, Outcome, List[tuple], int, int, str, str]:
                has_rv = task.node in active_resume
                return self._run_task(
                    task,
                    ctx,
                    wave_index=wave_index,
                    has_resume_value=has_rv,
                    resume_value=active_resume.get(task.node) if has_rv else None,
                )

            # 波次并发执行：普通单步直跑零开销；Send 扇出波次或多节点波次统一经 execute_wave（广播 wave.fanout/barrier）
            if len(wave) == 1 and not current_is_send_wave:
                ctx.current_node = wave[0].node
                wave_outcomes = [_exec_task(wave[0])]
            else:
                ctx.current_node = wave[0].node
                partial_outcomes: List[Tuple[Send, Outcome, List[tuple], int, int, str, str]] = []
                try:
                    wave_outcomes = execute_wave(
                        wave,
                        _exec_task,
                        bus=ctx.bus,
                        run_id=ctx.run.id,
                        stage=wave[0].node,
                        labels=[task.label or task.node for task in wave],
                        max_workers=wave_width,
                        cancellation=ctx.cancellation,
                        budget_guard=ctx.budget_guard,
                        partial_results_out=partial_outcomes,
                    )
                except BaseException:
                    if partial_outcomes:
                        ctx.check_wave_writes(
                            [(task.label or task.node, outcome.shared) for task, outcome, *_ in partial_outcomes]
                        )
                        partial_rejections = (
                            ctx.bus.drain_rejections() if hasattr(ctx.bus, "drain_rejections") else []
                        )
                        for rej in partial_rejections:
                            append_event(ctx.session, ctx.run, "BUS_REJECTED", wave[-1].node, rej, bus=ctx.bus)
                        for task, outcome, retry_events, *_ in partial_outcomes:
                            for kind, ev_node, payload in [*retry_events, *outcome.events]:
                                append_event(ctx.session, ctx.run, kind, ev_node, payload, bus=ctx.bus)
                            for key, value in outcome.shared.items():
                                ctx.put(key, value)
                        ctx.session.flush()
                        ctx.refresh_state()
                        ctx.session.commit()
                    raise

            # ── BSP 屏障：先原子检查同波次通道并发写冲突，再按确定性顺序落账与归并 Channel ──
            ctx.check_wave_writes(
                [(task.label or task.node, outcome.shared) for task, outcome, *_ in wave_outcomes]
            )

            bus_rejections = ctx.bus.drain_rejections() if hasattr(ctx.bus, "drain_rejections") else []
            barrier_node = wave[-1].node
            for rej in bus_rejections:
                append_event(ctx.session, ctx.run, "BUS_REJECTED", barrier_node, rej, bus=ctx.bus)

            interrupts_hit: List[Interrupt] = []
            goto_outcome: Optional[Tuple[str, Outcome]] = None
            next_sends: List[Send] = []

            for task, outcome, retry_events, *_ in wave_outcomes:
                ctx.current_node = task.node
                for kind, ev_node, payload in [*retry_events, *outcome.events]:
                    append_event(ctx.session, ctx.run, kind, ev_node, payload, bus=ctx.bus)
                if outcome.interrupt is not None:
                    intr = outcome.interrupt
                    if not intr.node:
                        intr = Interrupt(
                            kind=intr.kind,
                            node=task.node,
                            question=intr.question,
                            pending_input=dict(intr.pending_input),
                            resume_node=intr.resume_node or task.node,
                            identity=intr.identity,
                        )
                    interrupts_hit.append(intr)
                    append_event(
                        ctx.session,
                        ctx.run,
                        intr.kind,
                        intr.node,
                        intr.to_payload(),
                        bus=ctx.bus,
                    )
                for key, value in outcome.shared.items():
                    ctx.put(key, value)
                if outcome.sends:
                    for send_cmd in outcome.sends:
                        if send_cmd.node not in self._node_names:
                            raise ValueError(f"Send 目标不在 Plan 中：{send_cmd.node}")
                        next_sends.append(send_cmd)
                if outcome.goto and goto_outcome is None:
                    goto_outcome = (task.node, outcome)

            ctx.session.flush()
            ctx.refresh_state()

            for task, outcome, _, attempts, duration_ms, call_id, idempotency_key in wave_outcomes:
                if hasattr(ctx.bus, "fire"):
                    exit_meta: Dict[str, Any] = {
                        "run_id": ctx.run.id,
                        "node": task.node,
                        "when": outcome.when,
                        "goto": outcome.goto,
                        "sends": len(outcome.sends),
                        "interrupted": outcome.interrupt is not None,
                        "attempts": attempts,
                        "duration_ms": duration_ms,
                        "call_id": call_id,
                        "idempotency_key": idempotency_key,
                        "status": ctx.state.status,
                    }
                    if task.label:
                        exit_meta["label"] = task.label
                    ctx.bus.fire("node.exit", exit_meta)
                executed.append(
                    StepResult(
                        task.node,
                        outcome,
                        input=task.input,
                        label=task.label,
                        attempts=attempts,
                        duration_ms=duration_ms,
                        call_id=call_id,
                        idempotency_key=idempotency_key,
                    )
                )

            # 记录本波次产物快照，供下一波静态边自动按前驱形态喂入 input
            last_wave_nodes = [task.node for task, *_ in wave_outcomes]
            last_wave_whens = {task.node: outcome.when for task, outcome, *_ in wave_outcomes}
            last_wave_outputs = [(task.node, outcome.output) for task, outcome, *_ in wave_outcomes]
            last_wave_was_send = current_is_send_wave

            # 1) 一等 Interrupt：先提交事件真相，再广播 run.interrupted 并彻底放手
            if interrupts_hit:
                ctx.session.commit()
                if hasattr(ctx.bus, "fire"):
                    ctx.bus.fire(
                        "run.interrupted",
                        {
                            "run_id": ctx.run.id,
                            "node": interrupts_hit[0].node,
                            "interrupt_kind": interrupts_hit[0].kind,
                            "interrupts": [
                                {
                                    "node": item.node,
                                    "kind": item.kind,
                                    "resume_node": item.resume_node or item.node,
                                }
                                for item in interrupts_hit
                            ],
                            "status": ctx.state.status,
                        },
                    )
                return executed

            # 2) 终态优先止步：先提交真相再广播终态事件，且绝不被残留 Send/Goto 复活
            if ctx.state.status in {"COMPLETED", "FAILED"}:
                ctx.session.commit()
                if hasattr(ctx.bus, "fire"):
                    topic = "run.completed" if ctx.state.status == "COMPLETED" else "run.failed"
                    ctx.bus.fire(topic, {"run_id": ctx.run.id, "node": ctx.state.current_node, "status": ctx.state.status})
                return executed

            # 3) 动态扇出 Send：进入下一 BSP 波次
            if next_sends:
                pending_wave = next_sends
                is_send_wave = True
                cursor = wave[-1].node
            # 4) 运行时跳转 Goto：落 GO_REDIRECT 并把 control/output 喂给重入目标节点
            elif goto_outcome is not None:
                from_node, out = goto_outcome
                if out.goto not in self._node_names:
                    raise ValueError(f"goto 目标不在 Plan 中：{out.goto}")
                goto_payload = {"target": out.goto, "reason": "scheduler goto", **out.control}
                append_event(ctx.session, ctx.run, "GO_REDIRECT", from_node, goto_payload, bus=ctx.bus)
                ctx.session.flush()
                ctx.refresh_state()
                if hasattr(ctx.bus, "fire"):
                    ctx.bus.fire("node.redirect", {"run_id": ctx.run.id, "from": from_node, **goto_payload})
                goto_input = out.control if out.control else out.output
                pending_wave = [Send(out.goto, goto_input)]
                cursor = out.goto
            else:
                cursor = wave[-1].node
                last_when = wave_outcomes[-1][1].when

            # 波后取消 / 预算熔断：先提交已完成波次的检查点再抛出异常
            if ctx.is_cancelled:
                ctx.session.commit()
                ctx.check_cancelled()
            ctx.check_budget()
        else:
            # 跑满 max_steps 仍未到达终态或挂起态：检查是否还有待执行波次，若是则防空转熔断
            if not ctx.state.status.startswith("WAITING_") and ctx.state.status not in {"COMPLETED", "FAILED"}:
                remaining_wave = (
                    pending_wave
                    if pending_wave is not None
                    else self._build_static_wave(
                        cursor,
                        ctx,
                        last_when,
                        initial_input,
                        last_wave_nodes,
                        last_wave_whens,
                        last_wave_outputs,
                        last_wave_was_send,
                    )
                )
                runnable = [task.node for task in remaining_wave if task.node in self.bodies]
                if runnable:
                    ctx.session.commit()
                    raise MaxWavesExceededError(max_steps, cursor, runnable)

        ctx.session.commit()
        if hasattr(ctx.bus, "fire") and ctx.state.status.startswith("WAITING_"):
            ctx.bus.fire("run.interrupted", {"run_id": ctx.run.id, "node": ctx.state.current_node, "status": ctx.state.status})
        return executed
