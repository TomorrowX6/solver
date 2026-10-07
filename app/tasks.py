"""YesCaptcha / CapSolver 风格的任务接口:createTask、getTaskResult、getBalance。

    POST /createTask     {"clientKey": "...", "task": {"type": "TurnstileTaskProxyless", "websiteURL": "...", "websiteKey": "..."}}
    POST /getTaskResult  {"clientKey": "...", "taskId": "..."}

响应一律为 HTTP 200,用 errorId / errorCode 表示成败,与这类打码平台的客户端兼容。

taskId 为 UUID 格式;多副本部署时第一段是「位置编号 + worker 标识」(TS_TASK_PREFIX),
Cloudflare Worker 按第一位把查询路由回创建任务的 worker,worker 标识用来识别轮换前的旧任务。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .backend import BackendError
from .turnstile import TurnstileSolver

log = logging.getLogger("gateway.tasks")

# 不区分大小写;同时接受 YesCaptcha / 2Captcha / CapSolver 的类型名
# 不支持经调用方的代理求解:带代理的类型名(TurnstileTask)明确拒绝,而不是悄悄忽略代理
PROXYLESS_TYPES = {"turnstiletaskproxyless", "antiturnstiletaskproxyless"}
PROXY_TYPES = {"turnstiletask", "antiturnstiletask"}

# BackendError.code -> 打码平台通用错误码
_ERROR_CODES = {
    "busy": "ERROR_NO_SLOT_AVAILABLE",
    "solver_unavailable": "ERROR_SERVICE_UNAVALIABLE",
    "turnstile_error": "ERROR_CAPTCHA_UNSOLVABLE",
    "timeout": "ERROR_CAPTCHA_UNSOLVABLE",
    "page_error": "ERROR_CAPTCHA_UNSOLVABLE",
}


class TaskError(Exception):
    def __init__(self, code: str, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


def error_body(code: str, description: str) -> dict[str, Any]:
    return {"errorId": 1, "errorCode": code, "errorDescription": description}


@dataclass
class TaskParams:
    url: str
    sitekey: str
    action: str | None = None
    cdata: str | None = None


@dataclass
class Task:
    id: str
    params: TaskParams
    status: str = "processing"  # processing | ready | failed
    token: str | None = None
    user_agent: str | None = None
    error_code: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None


def _first(*values: Any) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def parse_task(task: Any) -> TaskParams:
    if not isinstance(task, dict):
        raise TaskError("ERROR_INVALID_TASK_DATA", "缺少 task 对象")
    kind = str(task.get("type") or "").lower()
    if kind in PROXY_TYPES:
        raise TaskError("ERROR_TASK_NOT_SUPPORTED", f"不支持带代理的 {task.get('type')}，请使用 TurnstileTaskProxyless")
    if kind not in PROXYLESS_TYPES:
        raise TaskError("ERROR_TASK_NOT_SUPPORTED", f"不支持的任务类型 {task.get('type')!r}，支持 TurnstileTaskProxyless")
    url = _first(task.get("websiteURL"), task.get("websiteUrl"))
    sitekey = _first(task.get("websiteKey"))
    if not url or not url.startswith(("http://", "https://")):
        raise TaskError("ERROR_INVALID_TASK_DATA", "websiteURL 缺失或不是 http(s) 地址")
    if not sitekey:
        raise TaskError("ERROR_INVALID_TASK_DATA", "websiteKey 缺失")
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    action = _first(task.get("action"), task.get("pageAction"), metadata.get("action"))
    cdata = _first(task.get("cdata"), task.get("data"), metadata.get("cdata"))
    return TaskParams(url=url, sitekey=sitekey, action=action, cdata=cdata)


def new_task_id(prefix: str) -> str:
    raw = uuid.uuid4().hex
    head = (prefix + raw)[:8]
    return f"{head}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


class TaskManager:
    def __init__(
        self,
        solver: TurnstileSolver,
        *,
        prefix: str,
        ttl: float,
        max_pending: int,
        timeout: float,
        attempt_timeout: float,
    ) -> None:
        self._solver = solver
        self.prefix = prefix
        self._ttl = ttl
        self._max_pending = max_pending
        self._timeout = timeout
        self._attempt_timeout = attempt_timeout
        self._tasks: dict[str, Task] = {}
        self._running: set[asyncio.Task] = set()
        # 已接收、还没进入浏览器名额等待的任务数(参与准入计算)
        self._not_started = 0
        self._janitor: asyncio.Task | None = None

    @property
    def pending(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == "processing")

    def start(self) -> None:
        self._janitor = asyncio.create_task(self._sweep_forever())

    async def stop(self) -> None:
        workers = [*self._running, *([self._janitor] if self._janitor else [])]
        for w in workers:
            w.cancel()
        for w in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w

    # ------------------------------------------------------------------ 接口
    def create(self, task: Any) -> str:
        params = parse_task(task)
        if not self._solver.available:
            raise TaskError("ERROR_SERVICE_UNAVALIABLE", "求解器暂不可用")
        if self.pending >= self._max_pending or not self._solver.slots.can_admit(extra=self._not_started):
            self._solver.slots.rejected += 1
            raise TaskError("ERROR_NO_SLOT_AVAILABLE", "当前没有空闲的识别名额，请稍后重试")
        item = Task(id=new_task_id(self.prefix), params=params)
        self._tasks[item.id] = item
        self._not_started += 1
        worker = asyncio.create_task(self._run(item))
        self._running.add(worker)
        worker.add_done_callback(self._running.discard)
        return item.id

    def result(self, task_id: Any) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise TaskError("ERROR_TASKID_INVALID", "缺少 taskId")
        if self.prefix and not task_id.startswith(self.prefix):
            # 同位置不同 worker 签发:创建它的 worker 已轮换下线,结果无法取回
            raise TaskError("ERROR_TASKID_INVALID", "任务不存在:创建该任务的 worker 已轮换下线，请重新创建")
        item = self._tasks.get(task_id)
        if item is None:
            raise TaskError("ERROR_TASKID_INVALID", "任务不存在或已过期")
        if item.status == "processing":
            return {"errorId": 0, "errorCode": None, "errorDescription": None, "status": "processing"}
        if item.status == "failed":
            return error_body(item.error_code or "ERROR_CAPTCHA_UNSOLVABLE", item.error or "识别失败")
        return {
            "errorId": 0,
            "errorCode": None,
            "errorDescription": None,
            "status": "ready",
            "solution": {"token": item.token, "userAgent": item.user_agent},
            "createTime": int(item.created * 1000),
            "endTime": int((item.finished or item.created) * 1000),
        }

    # ------------------------------------------------------------------ 执行与清理
    async def _run(self, item: Task) -> None:
        self._not_started -= 1
        p = item.params
        try:
            result = await self._solver.solve(
                p.url, p.sitekey, p.action, p.cdata, self._timeout, self._attempt_timeout, admit_check=False
            )
        except BackendError as e:
            item.status = "failed"
            item.error_code = _ERROR_CODES.get(e.code, "ERROR_CAPTCHA_UNSOLVABLE")
            item.error = e.message
        else:
            item.status = "ready"
            item.token = result.token
            item.user_agent = result.user_agent
        item.finished = time.time()

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, self._ttl))
            cutoff = time.time() - self._ttl
            for task_id in [k for k, t in self._tasks.items() if t.finished and t.finished < cutoff]:
                del self._tasks[task_id]
