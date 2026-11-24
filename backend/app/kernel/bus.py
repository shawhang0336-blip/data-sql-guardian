"""Bus：ProdAgent 全生命周期事件总线微内核（事前门禁裁决 check/adjudicate + 事后广播 fire/subscribe）。

机制层严守零业务硬编码：
1. 事前门禁裁决（check / adjudicate）：
   模型或节点准备执行动作前先过 Bus 征询已注册的 Adjudicator；任一裁决器返回拒绝原因即物理拦截，
   同时触发 `tool.rejected` 广播并在屏障处由 Scheduler 统一排空落 `BUS_REJECTED` 事件。
2. 全生命周期事后广播（fire / subscribe）：
   支持精确主题与 `*` 通配订阅，支持子总线（Run 级 `ctx.bus`）向父总线（`global_bus`）自动冒泡，
   且对旁观订阅者（Observer/Subscriber）做严格的异常隔离，绝不影响主流程状态机推进。
"""

import queue
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

Adjudicator = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]
Subscriber = Callable[[Dict[str, Any]], None]

LIFECYCLE_TOPICS = (
    "run.created",
    "run.resumed",
    "run.interrupted",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "node.enter",
    "node.retry",
    "node.exit",
    "node.redirect",
    "node.error",
    "wave.fanout",
    "wave.barrier",
    "tool.before",
    "tool.after",
    "tool.rejected",
    "budget.exceeded",
    "event.appended",
    "document.progress",
)

MAX_BUS_HISTORY = 2000


class Subscription:
    """有界流式订阅句柄：配合 `bus.stream(...)` 使用，防止高频事件撑爆内存。

    - `overflow="drop"`（默认）：队列满时丢弃新帧并累加 `dropped` 计数，绝不阻塞主流程；
    - `overflow="block"`：队列满时阻塞等待（可配 `block_timeout`）施加反压，尽量不丢帧。
    """

    def __init__(
        self,
        bus: "Bus",
        topic: str,
        *,
        maxsize: int = 256,
        overflow: str = "drop",
        block_timeout: Optional[float] = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("Subscription.maxsize 必须 >= 1")
        if overflow not in {"drop", "block"}:
            raise ValueError(f"未知 overflow 策略：{overflow}（仅支持 'drop' 或 'block'）")
        self._bus = bus
        self._topic = topic
        self._overflow = overflow
        self._block_timeout = block_timeout
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._lock = threading.Lock()
        self._closed = False
        self._bus.subscribe(topic, self._push)

    def _push(self, envelope: Dict[str, Any]) -> None:
        if self._closed:
            return
        if self._overflow == "block":
            try:
                self._queue.put(envelope, block=True, timeout=self._block_timeout)
                return
            except queue.Full:
                with self._lock:
                    self._dropped += 1
                return
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def get(self, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """取出下一条事件；超时无事件返回 None。"""
        try:
            return self._queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def get_nowait(self) -> Optional[Dict[str, Any]]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus.unsubscribe(self._topic, self._push)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()


class Bus:
    """全生命周期总线内核：事前门禁裁决（adjudicate/check）+ 事后主题广播（fire/subscribe）。"""

    def __init__(self, parent: Optional["Bus"] = None) -> None:
        if parent is self:
            raise ValueError("Bus.parent 不能指向自身")
        self._adjudicators: List[Adjudicator] = []
        self._observers: List[Subscriber] = []
        self._subscribers: Dict[str, List[Subscriber]] = defaultdict(list)
        self._rejections: List[Dict[str, Any]] = []
        self._history: List[Dict[str, Any]] = []
        self._seq: int = 0
        self._parent: Optional["Bus"] = parent
        self._lock = threading.Lock()
        self._budget_guard: Any = None

    def attach_budget(self, guard: Any) -> "Bus":
        """挂载横切预算守卫（BudgetGuard），供 LLM 守卫与调度器在关键节点做熔断检查。"""
        with self._lock:
            self._budget_guard = guard
        return self

    @property
    def budget_guard(self) -> Any:
        with self._lock:
            return self._budget_guard

    def check_budget(self) -> None:
        """触发当前总线及父总线挂载的预算检查；未挂载时为空操作。"""
        if self._parent is not None:
            self._parent.check_budget()
        guard = self.budget_guard
        if guard is not None and hasattr(guard, "check"):
            guard.check(self)

    def begin_llm_call(self) -> None:
        """在并发派发 LLM 前原子级联校验父/子预算并预占在途名额；子预算失败时自动回滚父预占。"""
        parent = self._parent
        parent_reserved = False
        if parent is not None:
            parent.begin_llm_call()
            parent_reserved = True
        guard = self.budget_guard
        try:
            if guard is not None and hasattr(guard, "begin_call"):
                guard.begin_call(self)
            elif guard is not None and hasattr(guard, "check"):
                guard.check(self)
        except Exception:
            if parent_reserved and parent is not None:
                parent.end_llm_calls(1)
            raise

    def end_llm_calls(self, count: int = 1) -> None:
        """归还已完成或异常退出的在途 LLM 调用名额，并级联归还父总线名额。"""
        guard = self.budget_guard
        if guard is not None and hasattr(guard, "end_calls"):
            guard.end_calls(count)
        if self._parent is not None:
            self._parent.end_llm_calls(count)

    def add_adjudicator(self, fn: Adjudicator) -> "Bus":
        with self._lock:
            if fn not in self._adjudicators:
                self._adjudicators.append(fn)
        return self

    def add_observer(self, fn: Subscriber) -> "Bus":
        """注册门禁拦截旁观者（兼容旧接口；等价于订阅 tool.rejected）。"""
        with self._lock:
            self._observers.append(fn)
        return self

    def subscribe(self, topic: str, fn: Subscriber) -> "Bus":
        """订阅特定生命周期主题（如 'node.enter'、'tool.rejected'、'event.appended'）或 '*' 通配全部主题。"""
        with self._lock:
            self._subscribers[topic].append(fn)
        return self

    def unsubscribe(self, topic: str, fn: Subscriber) -> "Bus":
        with self._lock:
            handlers = self._subscribers.get(topic, [])
            if fn in handlers:
                handlers.remove(fn)
        return self

    def stream(
        self,
        topic: str = "*",
        *,
        maxsize: int = 256,
        overflow: str = "drop",
        block_timeout: Optional[float] = None,
    ) -> Subscription:
        """创建有界队列流式订阅（支持 `drop` 丢帧计数与 `block` 反压），推荐配合 `with` 自动退订。"""
        return Subscription(
            self,
            topic,
            maxsize=maxsize,
            overflow=overflow,
            block_timeout=block_timeout,
        )

    def fire(self, topic: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """向外广播一条生命周期事件。旁观者抛出的任何异常均被吞掉隔离，绝不阻断主流程。"""
        with self._lock:
            self._seq += 1
            # 核心信封字段（seq / topic / timestamp）后写入，防止业务 payload 中的同名字段篡改总线元数据
            envelope: Dict[str, Any] = {
                **(payload or {}),
                "seq": self._seq,
                "topic": topic,
                "timestamp": time.time(),
            }
            self._history.append(envelope)
            if len(self._history) > MAX_BUS_HISTORY:
                del self._history[:-MAX_BUS_HISTORY]
            topic_handlers = list(self._subscribers.get(topic, []))
            wildcard_handlers = [] if topic == "*" else list(self._subscribers.get("*", []))
            parent = self._parent

        for handler in topic_handlers + wildcard_handlers:
            try:
                handler(envelope)
            except Exception:
                # 旁观者只看不拦：任何订阅者异常不得影响主流程
                pass

        if parent is not None:
            parent._forward(envelope)
        return envelope

    def _forward(self, envelope: Dict[str, Any]) -> None:
        """子 Bus（如单次 Run 的 ctx.bus）向父 Bus 逐级冒泡广播。"""
        topic = envelope.get("topic", "")
        with self._lock:
            self._seq += 1
            forwarded = {**envelope, "global_seq": self._seq}
            self._history.append(forwarded)
            if len(self._history) > MAX_BUS_HISTORY:
                del self._history[:-MAX_BUS_HISTORY]
            topic_handlers = list(self._subscribers.get(topic, []))
            wildcard_handlers = [] if topic == "*" else list(self._subscribers.get("*", []))
            parent = self._parent
        for handler in topic_handlers + wildcard_handlers:
            try:
                handler(forwarded)
            except Exception:
                pass
        if parent is not None:
            parent._forward(forwarded)

    def adjudicate(self, tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
        """事前门禁裁决：先发 tool.before 广播，再逐个跑裁决器；拒绝时发 tool.rejected 广播并返回原因。"""
        safe_payload = payload if isinstance(payload, dict) else {}
        table_obj = safe_payload.get("table")
        table_code = safe_payload.get("table_code") or (
            table_obj.get("table_code") if isinstance(table_obj, dict) else None
        )
        fields = safe_payload.get("fields") or []
        field_name = (
            fields[0].get("name")
            if isinstance(fields, list) and len(fields) == 1 and isinstance(fields[0], dict)
            else None
        )

        before_meta: Dict[str, Any] = {"tool": tool_name, "args": args}
        if table_code:
            before_meta["table_code"] = table_code
        if field_name:
            before_meta["field"] = field_name
        self.fire("tool.before", before_meta)

        with self._lock:
            adjudicators = list(self._adjudicators)

        for fn in adjudicators:
            adjudicator_name = getattr(fn, "__name__", "anonymous")
            try:
                reason = fn(tool_name, args, payload)
            except Exception as exc:  # noqa: BLE001
                # fail-closed：裁决器自身异常一律按拦截处理并留痕，绝不静默放行
                reason = f"门禁裁决器 {adjudicator_name} 执行异常（fail-closed 已拦截）：{type(exc).__name__}: {exc}"
            if reason:
                record: Dict[str, Any] = {
                    "tool": tool_name,
                    "adjudicator": adjudicator_name,
                    "reason": reason,
                    "args": args,
                }
                if table_code:
                    record["table_code"] = table_code
                if field_name:
                    record["field"] = field_name
                with self._lock:
                    self._rejections.append(record)
                    observers = list(self._observers)
                self.fire("tool.rejected", record)
                for observer in observers:
                    try:
                        observer(record)
                    except Exception:
                        pass
                return reason
        return None

    # ProdAgent 标准别名：check 等同于 adjudicate
    check = adjudicate

    def drain_rejections(self) -> List[Dict[str, Any]]:
        """取出并清空已累积的 Bus 拦截记录，供 Scheduler 屏障处统一落 BUS_REJECTED 事件。"""
        with self._lock:
            items = list(self._rejections)
            self._rejections.clear()
            return items

    def history(self, topic: Optional[str] = None, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询已广播的生命周期事件流（可按 topic 或 run_id 过滤）。"""
        with self._lock:
            items = list(self._history)
        if topic is not None:
            items = [item for item in items if item.get("topic") == topic]
        if run_id is not None:
            items = [item for item in items if item.get("run_id") == run_id]
        return items

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


_GLOBAL_BUS = Bus()


def global_bus() -> Bus:
    """进程级全局根 Bus：汇聚所有 Run 子总线冒泡的生命周期广播。"""
    return _GLOBAL_BUS
