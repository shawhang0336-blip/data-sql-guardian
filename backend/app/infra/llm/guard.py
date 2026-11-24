"""统一受控大模型调用守卫（基础设施层 Layer 0）。"""

from contextlib import contextmanager
import contextvars
import random
import re
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import httpx

from app.infra.errors import ProviderAuthOrBillingError, WorkflowError
from app.infra.llm.client import LlmTimeoutError, llm_rate_limiter, notify_llm_rate_limited, set_cancellation_provider

_ACTIVE_BUS: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("ds_active_llm_bus", default=None)
_ACTIVE_CANCEL: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("ds_active_llm_cancel", default=None)

DEFAULT_TRANSIENT_ATTEMPTS = 4
DEFAULT_VALIDATION_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)

_NO_TOOL_CALL_HINT = "上一次没有返回结构化工具调用，请务必调用指定工具返回合规 JSON 参数。"

_TRANSIENT_HTTP_MARKERS = ("LLM HTTP 429", "LLM HTTP 500", "LLM HTTP 502", "LLM HTTP 503", "LLM HTTP 504")
_FATAL_BILLING_MARKERS = (
    "LLM HTTP 401",
    "LLM HTTP 402",
    "LLM HTTP 403",
    "Insufficient Balance",
    "invalid_api_key",
)
_TRANSIENT_TRANSPORT_MARKERS = (
    "ConnectError",
    "ConnectTimeout",
    "ReadError",
    "WriteError",
    "RemoteProtocolError",
    "PoolTimeout",
    "TimeoutException",
    "ConnectionError",
)


def current_llm_bus() -> Optional[Any]:
    return _ACTIVE_BUS.get()


def current_cancellation() -> Optional[Any]:
    return _ACTIVE_CANCEL.get()


set_cancellation_provider(current_cancellation)


@contextmanager
def llm_execution_scope(*, bus: Any = None, cancellation: Any = None) -> Iterator[None]:
    bus_token = _ACTIVE_BUS.set(bus) if bus is not None else None
    cancel_token = _ACTIVE_CANCEL.set(cancellation) if cancellation is not None else None
    try:
        yield
    finally:
        if cancel_token is not None:
            _ACTIVE_CANCEL.reset(cancel_token)
        if bus_token is not None:
            _ACTIVE_BUS.reset(bus_token)


def submit_with_context(executor: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


def backoff_wait(
    steps: Tuple[float, ...],
    attempt: int,
    *,
    cancellation: Any = None,
    jitter: float = 0.2,
) -> None:
    index = min(max(attempt - 1, 0), len(steps) - 1)
    delay = float(steps[index]) if steps else 0.0
    if delay <= 0:
        return
    if jitter > 0:
        delay = max(0.0, delay * random.uniform(1.0 - jitter, 1.0 + jitter))
    if cancellation is not None and hasattr(cancellation, "wait"):
        if cancellation.wait(delay):
            cancellation.raise_if_cancelled()
        return
    time.sleep(delay)


def _classify_runtime_error(exc: RuntimeError) -> str:
    if isinstance(exc.__cause__, (httpx.HTTPError, TimeoutError, ConnectionError)):
        return "transport"
    text = str(exc)
    low = text.lower()
    if "no tool call" in low or "unexpected tool" in low:
        return "model_no_tool_call"
    if any(m in text for m in _TRANSIENT_HTTP_MARKERS) or any(m in text for m in _TRANSIENT_TRANSPORT_MARKERS):
        return "transport"
    return "fatal_model_error"


def call_llm_validated(
    llm: Any,
    tool_name: str,
    instruction: str,
    payload: Dict[str, Any],
    validator: Optional[Callable[[Dict[str, Any]], Any]] = None,
    *,
    timeout: Optional[int] = None,
    backoff: Optional[Tuple[float, ...]] = None,
    bus: Any = None,
    transient_attempts: int = DEFAULT_TRANSIENT_ATTEMPTS,
    validation_attempts: int = DEFAULT_VALIDATION_ATTEMPTS,
) -> Tuple[Any, Dict[str, Any]]:
    feedback: Optional[str] = None
    rejection: Optional[str] = None
    bus_rejections: List[Dict[str, Any]] = []
    last_kind = "validation"
    last_error = ""
    transient_used = 0
    validation_used = 0
    wait_steps = DEFAULT_BACKOFF_SECONDS if backoff is None else backoff
    active_bus = bus if bus is not None else _ACTIVE_BUS.get()
    cancellation = _ACTIVE_CANCEL.get()
    validate_fn = validator if validator is not None else (lambda args: args)

    max_transient = max(1, transient_attempts)
    max_total = max_transient + max(1, validation_attempts)
    rate_limit_bonus = 2
    reserved_calls = 0
    attempt = 0
    try:
        while attempt < (max_total + (rate_limit_bonus if "LLM HTTP 429" in last_error else 0)):
            attempt += 1
            if cancellation is not None and hasattr(cancellation, "raise_if_cancelled"):
                cancellation.raise_if_cancelled()
            if active_bus is not None and hasattr(active_bus, "begin_llm_call"):
                active_bus.begin_llm_call()
                reserved_calls += 1
            elif active_bus is not None and hasattr(active_bus, "check_budget"):
                active_bus.check_budget()
            attempt_timeout = timeout
            if timeout and transient_used:
                attempt_timeout = int(min(timeout * (1 + 0.5 * transient_used), timeout * 2))
            try:
                if attempt_timeout is None:
                    arguments, trace = (
                        llm.assign(tool_name, instruction, payload)
                        if feedback is None
                        else llm.assign(tool_name, instruction, payload, feedback)
                    )
                else:
                    arguments, trace = (
                        llm.assign(tool_name, instruction, payload, timeout=attempt_timeout)
                        if feedback is None
                        else llm.assign(tool_name, instruction, payload, feedback, timeout=attempt_timeout)
                    )
                if not isinstance(arguments, dict):
                    raise ValueError(f"工具 {tool_name} 必须返回 JSON 对象参数")

                rejection_reason = (
                    active_bus.adjudicate(tool_name, arguments, payload)
                    if active_bus is not None and hasattr(active_bus, "adjudicate")
                    else None
                )
                if rejection_reason:
                    bus_rejections.append(
                        {"attempt": attempt, "tool_name": tool_name, "reason": rejection_reason, "arguments": arguments}
                    )
                    if isinstance(trace, dict):
                        trace["bus_rejected"] = rejection_reason
                    raise ValueError(rejection_reason)

                result = validate_fn(arguments)
                if isinstance(trace, dict):
                    if bus_rejections:
                        trace["bus_rejected"] = bus_rejections[-1]["reason"]
                        trace["bus_rejections"] = bus_rejections
                    if rejection:
                        trace["previous_rejection"] = rejection
                    if attempt > 1:
                        trace["attempts"] = attempt
                if active_bus is not None and hasattr(active_bus, "fire"):
                    after_payload: Dict[str, Any] = {"tool": tool_name, "attempts": attempt}
                    if isinstance(trace, dict):
                        if trace.get("elapsed_seconds") is not None:
                            after_payload["elapsed_seconds"] = trace.get("elapsed_seconds")
                        if isinstance(trace.get("usage"), dict):
                            after_payload["usage"] = trace["usage"]
                    active_bus.fire("tool.after", after_payload)
                if reserved_calls and active_bus is not None and hasattr(active_bus, "end_llm_calls"):
                    active_bus.end_llm_calls(reserved_calls)
                    reserved_calls = 0
                return result, trace
            except LlmTimeoutError as exc:
                transient_used += 1
                last_kind, last_error = "timeout", str(exc)
                if transient_used >= transient_attempts:
                    break
                backoff_wait(wait_steps, transient_used, cancellation=cancellation)
            except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
                transient_used += 1
                last_kind, last_error = "transport", f"{type(exc).__name__}: {exc}"
                if transient_used >= transient_attempts:
                    break
                backoff_wait(wait_steps, transient_used, cancellation=cancellation)
            except RuntimeError as exc:
                kind = _classify_runtime_error(exc)
                last_error = str(exc)
                if kind == "fatal_model_error":
                    transient_used += 1
                    last_kind = "model_error"
                    break
                if kind == "model_no_tool_call":
                    validation_used += 1
                    last_kind = kind
                    feedback = _NO_TOOL_CALL_HINT
                    if validation_used >= validation_attempts:
                        break
                    continue
                transient_used += 1
                last_kind = kind
                is_429 = "LLM HTTP 429" in last_error
                if is_429 and llm_rate_limiter().cooldown_remaining() <= 0.0:
                    m_ra = re.search(r"retry_after=([0-9]+(?:\.[0-9]+)?)", last_error)
                    notify_llm_rate_limited(float(m_ra.group(1)) if m_ra else None)
                limit_transient = transient_attempts + (rate_limit_bonus if is_429 else 0)
                if transient_used >= limit_transient:
                    break
                backoff_wait(wait_steps, transient_used, cancellation=cancellation)
            except (ValueError, KeyError) as exc:
                validation_used += 1
                rejection = f"缺少必填字段 {exc}" if isinstance(exc, KeyError) else str(exc)
                feedback = rejection
                last_kind, last_error = "validation", rejection
                if validation_used >= validation_attempts:
                    break
    finally:
        if reserved_calls and active_bus is not None and hasattr(active_bus, "end_llm_calls"):
            active_bus.end_llm_calls(reserved_calls)

    if last_kind == "model_error" and any(m.lower() in last_error.lower() for m in _FATAL_BILLING_MARKERS):
        raise ProviderAuthOrBillingError(
            f"{tool_name} 调用失败（{last_kind}）：{last_error}",
            context={"tool_name": tool_name, "error_kind": last_kind},
        )
    raise WorkflowError(
        f"{tool_name} 调用失败（{last_kind}）：{last_error}",
        context={"tool_name": tool_name, "error_kind": last_kind, "retried": transient_used > 1},
    )
