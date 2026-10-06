"""FlareSolverr 客户端:并发与排队控制、会话跟踪和闲置会话清理。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("gateway.backend")

# 这些命令每次都会启动浏览器,占用并发槽位
HEAVY_COMMANDS = {"request.get", "request.post", "sessions.create"}


class BackendError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Slots:
    """浏览器并发名额:/v1 的耗时命令与 /solve 共用,保证同时运行的浏览器数量不超过上限。"""

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
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None, slots: Slots | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=settings.backend_url, transport=transport, timeout=30)
        self.slots = slots or Slots(settings.max_concurrency, settings.max_queue)
        # 会话 ID -> 最近一次使用时间(monotonic)
        self.sessions: dict[str, float] = {}
        self.version: str | None = None
        self.completed = 0
        self.failed = 0
        self._janitor: asyncio.Task | None = None

    async def start(self) -> None:
        if self._settings.session_idle_ttl > 0:
            self._janitor = asyncio.create_task(self._sweep_forever())

    async def stop(self) -> None:
        if self._janitor:
            self._janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._janitor
        await self._client.aclose()

    # ------------------------------------------------------------------ 状态
    async def healthy(self) -> bool:
        try:
            resp = await self._client.get("/health", timeout=5)
            return resp.status_code == 200 and resp.json().get("status") == "ok"
        except (httpx.HTTPError, ValueError):
            return False

    # ------------------------------------------------------------------ 转发
    async def call(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """转发一条 /v1 命令,返回 (HTTP 状态码, FlareSolverr 的响应 JSON)。"""
        cmd = payload.get("cmd")
        session = payload.get("session")

        if cmd in ("request.get", "request.post"):
            limit_ms = int(self._settings.max_timeout * 1000)
            try:
                requested = int(payload.get("maxTimeout") or 60000)
            except (TypeError, ValueError):
                requested = 60000
            payload["maxTimeout"] = max(1000, min(requested, limit_ms))

        if cmd == "sessions.create" and self._settings.max_sessions and session not in self.sessions:
            if len(self.sessions) >= self._settings.max_sessions:
                self.slots.rejected += 1
                raise BackendError("session_limit", f"会话数已达上限 {self._settings.max_sessions}，请先销毁不用的会话")

        if cmd not in HEAVY_COMMANDS:
            status, body = await self._post(payload, timeout=30)
        else:
            async with self.slots.hold():
                timeout = int(payload.get("maxTimeout") or 60000) / 1000 + 20
                status, body = await self._post(payload, timeout=timeout)
            if status == 200 and body.get("status") == "ok":
                self.completed += 1
            else:
                self.failed += 1

        self._track_session(cmd, session, status, body)
        return status, body

    async def _post(self, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
        try:
            resp = await self._client.post("/v1", json=payload, timeout=timeout)
        except httpx.TimeoutException:
            raise BackendError("backend_timeout", "等待 FlareSolverr 响应超时") from None
        except httpx.HTTPError as e:
            raise BackendError("backend_unavailable", f"FlareSolverr 不可用: {type(e).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            raise BackendError("backend_error", f"FlareSolverr 返回了非 JSON 响应(HTTP {resp.status_code})") from None
        if isinstance(body, dict) and body.get("version"):
            self.version = body["version"]
        return resp.status_code, body if isinstance(body, dict) else {"result": body}

    def _track_session(self, cmd: Any, session: Any, status: int, body: dict[str, Any]) -> None:
        now = time.monotonic()
        if cmd == "sessions.create" and status == 200:
            self.sessions[str(body.get("session") or session)] = now
        elif cmd == "sessions.destroy" and session:
            self.sessions.pop(str(session), None)
        elif cmd in ("request.get", "request.post") and session:
            # FlareSolverr 遇到不存在的会话会自动创建,这里同样记下
            self.sessions[str(session)] = now
        elif cmd == "sessions.list" and status == 200:
            self._reconcile(body.get("sessions") or [], now)

    def _reconcile(self, live: list[str], now: float) -> None:
        live_set = {str(s) for s in live}
        for sid in list(self.sessions):
            if sid not in live_set:
                del self.sessions[sid]
        for sid in live_set:
            self.sessions.setdefault(sid, now)

    # ------------------------------------------------------------------ 闲置会话清理
    async def sweep(self) -> list[str]:
        status, body = await self._post({"cmd": "sessions.list"}, timeout=15)
        if status != 200:
            return []
        now = time.monotonic()
        self._reconcile(body.get("sessions") or [], now)
        expired = [sid for sid, used in self.sessions.items() if now - used > self._settings.session_idle_ttl]
        for sid in expired:
            await self._post({"cmd": "sessions.destroy", "session": sid}, timeout=30)
            self.sessions.pop(sid, None)
            log.info("destroyed idle session %s", sid)
        return expired

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self._settings.session_idle_ttl))
            try:
                await self.sweep()
            except BackendError as e:
                log.warning("session sweep failed: %s", e.message)
