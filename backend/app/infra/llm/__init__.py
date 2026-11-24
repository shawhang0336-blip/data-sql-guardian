"""受控大模型基础设施子包（Layer 0）。"""

from .client import (
    LlmTimeoutError,
    RealDesignLlm,
    TokenBucketRateLimiter,
    close_http_client,
    configure_llm_concurrency,
    configure_llm_rate_limit,
    llm_call_semaphore,
    llm_rate_limiter,
    notify_llm_rate_limited,
)
from .guard import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_TRANSIENT_ATTEMPTS,
    DEFAULT_VALIDATION_ATTEMPTS,
    backoff_wait,
    call_llm_validated,
    current_cancellation,
    current_llm_bus,
    llm_execution_scope,
    submit_with_context,
)
from .schemas import TOOL_SCHEMAS, tool_prompt_preamble

__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_TRANSIENT_ATTEMPTS",
    "DEFAULT_VALIDATION_ATTEMPTS",
    "LlmTimeoutError",
    "RealDesignLlm",
    "TOOL_SCHEMAS",
    "TokenBucketRateLimiter",
    "backoff_wait",
    "call_llm_validated",
    "close_http_client",
    "configure_llm_concurrency",
    "configure_llm_rate_limit",
    "current_cancellation",
    "current_llm_bus",
    "llm_call_semaphore",
    "llm_execution_scope",
    "llm_rate_limiter",
    "notify_llm_rate_limited",
    "submit_with_context",
    "tool_prompt_preamble",
]
