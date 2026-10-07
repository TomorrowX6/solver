"""浏览器并发名额,以及 FlareSolverr 进程的健康检查(求解器复用它的反检测浏览器)。"""

from __future__ import annotations

import asyncio
import contextlib

import httpx

from .config import Settings


class BackendError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Slots:
    """浏览器并发名额:/solve 与异步任务共用,保证同时运行的浏览器数量不超过上限。"""

    def __init__(self, capacity: int, max_queue: int) -> None:
        self.capacity = capacity
        self.max_queue = max_queue
        self._sem = asyncio.Semaphore(capacity)
        self.active = 0
        self.waiting = 0
        self.rejected = 0

    def can_admit(self, extra: int = 0) -> bool:
        """extra:已接收但还没进入等待的请求数(如刚创建、尚未开始的异步任务)。"""
        if not self.max_queue:
            return True
        return (self.capacity - self.active) + self.max_queue - self.waiting - extra > 0

    @contextlib.asynccontextmanager
    async def hold(self, check: bool = True):
        """check=False 用于提交时已做过准入检查的异步任务,不会在这里被拒绝。"""
        if check and not self.can_admit():
            self.rejected += 1
            raise BackendError("busy", "并发槽位已满且排队已满，请稍后重试")
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._sem.release()


class FlareSolverr:
    """只做健康检查:网关不再向 FlareSolverr 转发请求(5 秒盾已下线)。"""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=settings.backend_url, transport=transport, timeout=5)
        self.version: str | None = None

    async def stop(self) -> None:
        await self._client.aclose()

    async def healthy(self) -> bool:
        try:
            resp = await self._client.get("/health")
            ok = resp.status_code == 200 and resp.json().get("status") == "ok"
            if ok and self.version is None:
                # 版本号只在首页返回
                self.version = (await self._client.get("/")).json().get("version")
            return ok
        except (httpx.HTTPError, ValueError, AttributeError):
            return False
