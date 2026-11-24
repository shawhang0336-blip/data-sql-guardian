"""领域与受控大模型调用异常定义（基础设施层 Layer 0）。"""

from typing import Any, Dict, Optional


class WorkflowError(RuntimeError):
    """受控工作流异常，携带定位与诊断上下文。"""

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.context = context or {}


class ProviderAuthOrBillingError(RuntimeError):
    """上游大模型鉴权失效或余额不足（HTTP 401/402/403），需安全暂停批任务而非静默降级。"""

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.context = context or {}
