"""进程内有界后台任务执行器（TaskRunner，按任务键去重，基础设施层 Layer 0）。"""

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from typing import Any, Callable, List, Optional, Set

logger = logging.getLogger(__name__)


class TaskRunner:
    """带任务键去重的有界后台线程池。"""

    def __init__(self, max_workers: int = 4, *, inline: bool = False) -> None:
        self._max_workers = max(1, int(max_workers))
        self._inline = inline
        self._lock = threading.Lock()
        self._active: Set[str] = set()
        self._executor: Optional[ThreadPoolExecutor] = None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="ds-agent-bg",
            )
        return self._executor

    def is_active(self, key: str) -> bool:
        with self._lock:
            return key in self._active

    def active_keys(self) -> List[str]:
        with self._lock:
            return sorted(self._active)

    def submit(self, key: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)

        def _wrapped() -> None:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                logger.exception("Background task failed: %s", key)
            finally:
                with self._lock:
                    self._active.discard(key)

        if self._inline:
            _wrapped()
            return True

        try:
            with self._lock:
                executor = self._get_executor()
            executor.submit(_wrapped)
        except Exception:
            with self._lock:
                self._active.discard(key)
            raise
        return True

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=not wait)


_GLOBAL_RUNNER = TaskRunner(max_workers=4)


def task_runner() -> TaskRunner:
    return _GLOBAL_RUNNER
