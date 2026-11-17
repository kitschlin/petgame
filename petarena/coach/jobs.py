"""后台任务：对局结束后的入库、定向推演、讲解、培养方案，都不该让玩家等。

ThreadRunner 用一条后台线程按提交顺序逐个执行（先进先出，所以同一场对局的几步不会乱序）；
InlineRunner 当场执行，测试和脚本用它得到确定的结果。任务出错只记下来，不影响对局。
"""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Any, Callable


class InlineRunner:
    """当场执行。"""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            self.errors.append(traceback.format_exc())

    def drain(self, timeout: float | None = None) -> bool:
        return True

    @property
    def busy(self) -> bool:
        return False


class ThreadRunner:
    """一条后台线程，按提交顺序执行。第一次提交任务时才启动线程。"""

    def __init__(self, name: str = "coach-jobs") -> None:
        self.errors: list[str] = []
        self._name = name
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        self._queue.put((fn, args))
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name=self._name, daemon=True)
                self._thread.start()

    def _work(self) -> None:
        while True:
            fn, args = self._queue.get()
            try:
                fn(*args)
            except Exception:
                self.errors.append(traceback.format_exc())
                del self.errors[:-20]
            finally:
                self._queue.task_done()

    def drain(self, timeout: float | None = None) -> bool:
        """等到队列里的任务都做完（脚本和测试用）。超时返回 False。"""
        done = threading.Event()

        def wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=wait, daemon=True).start()
        return done.wait(timeout)

    @property
    def busy(self) -> bool:
        return self._queue.unfinished_tasks > 0
