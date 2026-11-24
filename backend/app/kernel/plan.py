"""WorkflowPlan：ProdAgent 静态工作流蓝图微内核（Plan）。

职责边界：
- Plan 回答静态问题：有哪些节点、节点怎么连、哪条边在什么条件下走、节点的超时与退避重试策略是什么；
- 不可变、可被多个 Run 复用、可在运行前做悬空边/孤岛节点/终态出边静态校验、可渲染成 Mermaid 图；
- 机制层不定义任何业务节点名（业务拓扑位于具体策略层）。
"""

from collections import deque
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Type, Union


@dataclass(frozen=True)
class RetryPolicy:
    """节点级声明式重试与指数退避策略（机制在内执行，重试条件与退避参数由 Plan 声明）。

    - `max_attempts`：含首次执行在内的最大尝试次数；
    - `base_delay` / `factor` / `max_delay`：第 n 次失败（从 0 起）等待 `base_delay * factor**n` 秒；
    - `jitter`：随机抖动比例（默认 `0.2`，即在 `[1-jitter, 1+jitter]` 区间内错峰退避，防止并发波次惊群效应）；
    - `retry_on`：允许重试的异常类型元组或谓词函数；取消（`RunCancelledError`）、预算熔断（`BudgetExceededError`）
      与通道并发写冲突（`AmbiguousWriteError`）永不重试。
    """

    max_attempts: int = 3
    base_delay: float = 0.05
    factor: float = 2.0
    max_delay: Optional[float] = None
    jitter: float = 0.2
    retry_on: Union[Tuple[Type[BaseException], ...], Callable[[BaseException], bool]] = (Exception,)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts 必须 >= 1")
        if self.base_delay < 0:
            raise ValueError("RetryPolicy.base_delay 不能为负数")
        if self.factor < 1:
            raise ValueError("RetryPolicy.factor 必须 >= 1")
        if self.max_delay is not None and self.max_delay < 0:
            raise ValueError("RetryPolicy.max_delay 不能为负数")
        if self.jitter < 0 or self.jitter > 1:
            raise ValueError("RetryPolicy.jitter 必须在 [0, 1] 之间")
        if isinstance(self.retry_on, tuple):
            if not self.retry_on or not all(
                isinstance(item, type) and issubclass(item, BaseException) for item in self.retry_on
            ):
                raise ValueError("RetryPolicy.retry_on 元组必须非空且元素均为异常类型")
        elif isinstance(self.retry_on, type) and issubclass(self.retry_on, BaseException):
            raise ValueError(
                f"RetryPolicy.retry_on 不能直接传单个异常类 {self.retry_on.__name__}，"
                f"请改用单元素元组 ({self.retry_on.__name__},)"
            )
        elif not callable(self.retry_on):
            raise ValueError("RetryPolicy.retry_on 须为异常类型元组或可调用谓词函数")

    def delay_for(
        self,
        failed_attempt: int,
        *,
        rng: Optional[Callable[[float, float], float]] = None,
    ) -> float:
        delay = self.base_delay * (self.factor ** max(0, failed_attempt))
        if self.jitter > 0 and delay > 0:
            sampler = rng or random.uniform
            delay = max(0.0, delay * sampler(1.0 - self.jitter, 1.0 + self.jitter))
        if self.max_delay is not None:
            delay = min(delay, self.max_delay)
        return delay

    def matches(self, exc: BaseException) -> bool:
        if isinstance(self.retry_on, tuple):
            return isinstance(exc, self.retry_on)
        return bool(self.retry_on(exc))


@dataclass(frozen=True)
class PlanNode:
    name: str
    label: str
    kind: str  # stage | gate | terminal
    timeout: Optional[float] = None
    retry: Optional[RetryPolicy] = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("节点名称不能为空")
        if self.kind not in {"stage", "gate", "terminal"}:
            raise ValueError(f"未知节点类型：{self.kind}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"节点 {self.name} 的 timeout 必须大于 0")


@dataclass(frozen=True)
class PlanEdge:
    src: str
    dst: str
    when: str  # 条件标签，由执行器判定；"always" 为无条件边

    def __post_init__(self) -> None:
        if not self.src or not self.src.strip() or not self.dst or not self.dst.strip():
            raise ValueError("边不能有空端点")
        if not self.when or not self.when.strip():
            raise ValueError(f"边 {self.src} -> {self.dst} 的条件标签 when 不能为空")


@dataclass(frozen=True)
class WorkflowPlan:
    name: str
    nodes: Tuple[PlanNode, ...]
    edges: Tuple[PlanEdge, ...]
    entry: str

    def __post_init__(self) -> None:
        errors = self.validate()
        if errors:
            raise ValueError("非法 Plan：" + "；".join(errors))

    def validate(self) -> List[str]:
        """静态校验：重名节点、入口缺失、悬空边、重复边、终态出边、无出边阶段节点、不可达节点。"""
        errors: List[str] = []
        names = [node.name for node in self.nodes]
        if len(names) != len(set(names)):
            errors.append("存在重复节点")
        name_set = set(names)
        if self.entry not in name_set:
            errors.append(f"入口节点 {self.entry} 不存在")
        seen_edges = set()
        for edge in self.edges:
            if edge.src not in name_set:
                errors.append(f"悬空边：{edge.src} -[{edge.when}]-> {edge.dst} 的起点不存在")
            if edge.dst not in name_set:
                errors.append(f"悬空边：{edge.src} -[{edge.when}]-> {edge.dst} 的终点不存在")
            edge_key = (edge.src, edge.dst, edge.when)
            if edge_key in seen_edges:
                errors.append(f"重复边：{edge.src} -[{edge.when}]-> {edge.dst}")
            else:
                seen_edges.add(edge_key)
        terminals = {node.name for node in self.nodes if node.kind == "terminal"}
        outgoing: Dict[str, List[str]] = {}
        for edge in self.edges:
            if edge.src in terminals:
                errors.append(f"终态节点 {edge.src} 不应有出边")
            outgoing.setdefault(edge.src, []).append(edge.dst)

        dead_end_stages = [
            node.name for node in self.nodes if node.kind == "stage" and not outgoing.get(node.name)
        ]
        if dead_end_stages:
            errors.append(f"非终态阶段节点缺少出边：{sorted(dead_end_stages)}")

        # 仅当入口节点合法时做可达性分析，避免入口写错时把所有正常节点误报为不可达
        if self.entry in name_set:
            seen = {self.entry}
            queue: deque[str] = deque([self.entry])
            while queue:
                current = queue.popleft()
                for dst in outgoing.get(current, []):
                    if dst in name_set and dst not in seen:
                        seen.add(dst)
                        queue.append(dst)
            unreachable = name_set - seen
            if unreachable:
                errors.append(f"不可达节点：{sorted(unreachable)}")
        return errors

    def node(self, name: str) -> PlanNode:
        for item in self.nodes:
            if item.name == name:
                return item
        raise KeyError(f"Plan 中没有节点 {name}")

    def next(self, name: str, when: str = "always") -> Optional[str]:
        """按节点名 + 条件标签查后继；找不到返回 None（中断点或终态）。"""
        for edge in self.edges:
            if edge.src == name and edge.when == when:
                return edge.dst
        return None

    def successors(self, name: str) -> Tuple[PlanEdge, ...]:
        return tuple(edge for edge in self.edges if edge.src == name)

    def render_mermaid(self) -> str:
        """渲染成 Mermaid flowchart，便于可视化与人工核对拓扑。"""
        lines = ["flowchart TD"]
        for node in self.nodes:
            safe_label = node.label.replace('"', "'")
            if node.kind == "gate":
                lines.append(f'  {node.name}{{{{"{node.name} {safe_label}"}}}}')
            elif node.kind == "terminal":
                lines.append(f'  {node.name}(( "{node.name} {safe_label}" ))')
            else:
                lines.append(f'  {node.name}["{node.name} {safe_label}"]')
        for edge in self.edges:
            label = "" if edge.when == "always" else edge.when.replace("|", "/")
            lines.append(f"  {edge.src} -->|{label}| {edge.dst}")
        return "\n".join(lines)
