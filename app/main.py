"""Turnstile / FlareSolverr 网关。

    POST /createTask、/getTaskResult、/getBalance   YesCaptcha / CapSolver 风格的任务接口(clientKey = API Key)
    POST /solve   传入 url + sitekey,同步返回 Turnstile token(复用 FlareSolverr 的反检测浏览器)
    POST /v1      FlareSolverr 接口,请求与响应格式不变

两者共用一套浏览器并发名额,并提供鉴权、排队上限、超时上限和闲置会话清理。
网关自身的错误统一为 {"status": "error", "message": ..., "code": ...}。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .backend import BackendError, FlareSolverr, Slots
from .config import Settings
from .models import Health, SolveRequest, SolveResponse
from .tasks import TaskError, TaskManager, error_body
from .turnstile import TurnstileSolver

log = logging.getLogger("gateway.api")

# 网关错误码 -> HTTP 状态码。不使用 502/504:部署在 Cloudflare 后面时,
# 源站返回的这两个状态码会被替换成 Cloudflare 自己的错误页,JSON 错误信息会丢失
_ERROR_STATUS = {
    "unauthorized": 401,
    "invalid_request": 400,
    "invalid_proxy": 400,
    "turnstile_error": 422,
    "busy": 429,
    "session_limit": 429,
    # FlareSolverr / 求解器不可用,请求尚未执行:上游可以改投其他副本
    "backend_unavailable": 503,
    "solver_unavailable": 503,
    # 已实际执行但没拿到 token:按最终结果返回,避免上游重复执行耗时请求
    "timeout": 500,
    "page_error": 500,
    # 已交给 FlareSolverr 处理但超时或返回异常:按最终结果返回,避免重复执行耗时请求
    "backend_timeout": 500,
    "backend_error": 500,
}


def _error(code: str, message: str) -> JSONResponse:
    status = _ERROR_STATUS.get(code, 500)
    headers = {"Retry-After": "2"} if status in (429, 503) else None
    return JSONResponse(status_code=status, content={"status": "error", "message": message, "code": code}, headers=headers)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


def _container_stats() -> tuple[float | None, float | None, int | None]:
    """返回 (累计 CPU 秒数, CPU 核数上限, 已用内存 MB),数据来自容器自身的 cgroup;非 Linux 返回 None。"""
    cpu_seconds = cpu_limit = mem_mb = None
    if stat := _read("/sys/fs/cgroup/cpu.stat"):  # cgroup v2
        for line in stat.splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec":
                cpu_seconds = int(value) / 1e6
        if (cpu_max := _read("/sys/fs/cgroup/cpu.max")) and not cpu_max.startswith("max"):
            quota, period = cpu_max.split()
            cpu_limit = int(quota) / int(period)
        if mem := _read("/sys/fs/cgroup/memory.current"):
            mem_mb = int(mem) // 2**20
    elif usage := _read("/sys/fs/cgroup/cpuacct/cpuacct.usage"):  # cgroup v1
        cpu_seconds = int(usage) / 1e9
        quota, period = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota and period and int(quota) > 0:
            cpu_limit = int(quota) / int(period)
        if mem := _read("/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            mem_mb = int(mem) // 2**20
    if cpu_limit is None and cpu_seconds is not None:
        cpu_limit = float(os.cpu_count() or 1)
    return (round(cpu_seconds, 2) if cpu_seconds is not None else None), cpu_limit, mem_mb


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        slots = Slots(settings.max_concurrency, settings.max_queue)
        backend = FlareSolverr(settings, transport=transport, slots=slots)
        solver = TurnstileSolver(
            slots,
            settings.max_concurrency,
            settings.flaresolverr_dir,
            settings.solve_page,
            settings.solve_inject,
            settings.click_mode,
            settings.debug_dir,
            settings.chrome_args,
            settings.chrome_args_file,
            settings.stall_seconds,
        )
        tasks = TaskManager(
            solver,
            prefix=settings.task_prefix,
            ttl=settings.task_ttl,
            max_pending=settings.max_pending_tasks,
            timeout=settings.max_timeout,
            attempt_timeout=settings.attempt_timeout,
        )
        await backend.start()
        await solver.start()
        tasks.start()
        app.state.backend = backend
        app.state.solver = solver
        app.state.tasks = tasks
        try:
            yield
        finally:
            await tasks.stop()
            await solver.stop()
            await backend.stop()

    app = FastAPI(
        title="Turnstile Solver Gateway",
        version="3.0.0",
        description=(
            "POST /createTask + /getTaskResult:YesCaptcha / CapSolver 风格的 Turnstile 任务接口;"
            "POST /solve:传入 url + sitekey 同步返回 token;POST /v1:FlareSolverr 兼容接口。"
        ),
        lifespan=lifespan,
    )

    def authorized(request: Request) -> bool:
        if not settings.api_key:
            return True
        provided = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if not provided and auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        return bool(provided) and secrets.compare_digest(provided.encode(), settings.api_key.encode())

    def backend_of(request: Request) -> FlareSolverr:
        return request.app.state.backend

    def solver_of(request: Request) -> TurnstileSolver:
        return request.app.state.solver

    @app.get("/", tags=["meta"])
    async def index(request: Request) -> dict:
        return {"msg": "FlareSolverr gateway is ready!", "version": backend_of(request).version}

    @app.get("/health", response_model=Health, tags=["meta"])
    async def health(request: Request) -> Health:
        backend = backend_of(request)
        solver = solver_of(request)
        backend_ok = await backend.healthy()
        cpu_seconds, cpu_limit, mem_used_mb = _container_stats()
        return Health(
            status="ok" if backend_ok and solver.available else "degraded",
            worker=settings.worker_id or None,
            backend="ok" if backend_ok else "down",
            backend_version=backend.version,
            solver="ok" if solver.available else "unavailable",
            solver_error=None if solver.available else getattr(solver, "unavailable_reason", None),
            active=backend.slots.active,
            capacity=settings.max_concurrency,
            queued=backend.slots.waiting,
            completed=backend.completed,
            failed=backend.failed,
            solved=solver.solved,
            solve_failed=solver.failed,
            rejected=backend.slots.rejected,
            sessions=len(backend.sessions),
            tasks_pending=request.app.state.tasks.pending,
            draining=draining(),
            attempt_errors=dict(getattr(solver, "attempt_errors", {})),
            token_stats=dict(getattr(solver, "token_stats", {})),
            cpu_seconds=cpu_seconds,
            cpu_limit=cpu_limit,
            mem_used_mb=mem_used_mb,
        )

    @app.get("/admin/stats", tags=["meta"], summary="后台面板数据:健康状态、守护进程状态、最近的求解与每分钟计数")
    async def admin_stats(request: Request):
        if not authorized(request):
            return _error("unauthorized", "缺少或错误的 API Key")
        agent = None
        if settings.agent_state_file:
            try:
                with open(settings.agent_state_file, encoding="utf-8") as f:
                    agent = json.load(f)
            except (OSError, ValueError):
                agent = None
        if agent:
            agent.pop("tunnel_token", None)  # 只是来源变量名,面板用不到
        return {
            "now": round(time.time(), 1),
            "health": (await health(request)).model_dump(),
            "agent": agent,
            **solver_of(request).history.snapshot(),
        }

    def draining() -> bool:
        return bool(settings.drain_file) and os.path.exists(settings.drain_file)

    def key_ok(key: Any) -> bool:
        if not settings.api_key:
            return True
        return isinstance(key, str) and bool(key) and secrets.compare_digest(key.encode(), settings.api_key.encode())

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise TaskError("ERROR_INVALID_TASK_DATA", "请求体不是合法的 JSON") from None
        if not isinstance(body, dict):
            raise TaskError("ERROR_INVALID_TASK_DATA", "请求体必须是 JSON 对象")
        return body

    @app.post("/createTask", tags=["tasks"], summary="创建 Turnstile 任务(TurnstileTaskProxyless / TurnstileTask)")
    async def create_task(request: Request) -> dict:
        try:
            body = await json_body(request)
            if not key_ok(body.get("clientKey")):
                raise TaskError("ERROR_KEY_DOES_NOT_EXIST", "clientKey 错误")
            if draining():
                raise TaskError("ERROR_NO_SLOT_AVAILABLE", "该 worker 正在下线，请重试")
            task_id = request.app.state.tasks.create(body.get("task"))
        except TaskError as e:
            return error_body(e.code, e.description)
        return {"errorId": 0, "errorCode": "", "errorDescription": "", "taskId": task_id}

    @app.post("/getTaskResult", tags=["tasks"], summary="查询任务结果:processing 时 3 秒后再查")
    async def get_task_result(request: Request) -> dict:
        try:
            body = await json_body(request)
            if not key_ok(body.get("clientKey")):
                raise TaskError("ERROR_KEY_DOES_NOT_EXIST", "clientKey 错误")
            return request.app.state.tasks.result(body.get("taskId"))
        except TaskError as e:
            return error_body(e.code, e.description)

    @app.post("/getBalance", tags=["tasks"], summary="余额(自建服务不计费，固定返回)")
    async def get_balance(request: Request) -> dict:
        try:
            body = await json_body(request)
        except TaskError as e:
            return error_body(e.code, e.description)
        if not key_ok(body.get("clientKey")):
            return error_body("ERROR_KEY_DOES_NOT_EXIST", "clientKey 错误")
        return {"errorId": 0, "errorCode": "", "errorDescription": "", "balance": 999999}

    @app.post(
        "/solve",
        response_model=SolveResponse,
        tags=["turnstile"],
        summary="传入 url + sitekey，直接返回 Turnstile token",
    )
    async def solve(body: SolveRequest, request: Request):
        if not authorized(request):
            return _error("unauthorized", "缺少或错误的 API Key")
        if draining():
            return _error("busy", "该 worker 正在下线，请重试")
        timeout = min(body.timeout or settings.default_timeout, settings.max_timeout)
        try:
            result = await solver_of(request).solve(
                str(body.url), body.sitekey, body.action, body.cdata, body.proxy, timeout, settings.attempt_timeout
            )
        except BackendError as e:
            log.warning("solve %s rejected [%s]: %s", body.url, e.code, e.message)
            return _error(e.code, e.message)
        return SolveResponse(
            token=result.token, elapsed=result.elapsed, attempts=result.attempts, user_agent=result.user_agent
        )

    @app.post("/v1", tags=["flaresolverr"], summary="FlareSolverr v1 接口(请求与响应格式同 FlareSolverr)")
    async def v1(request: Request) -> JSONResponse:
        if not authorized(request):
            return _error("unauthorized", "缺少或错误的 API Key")
        try:
            payload = await request.json()
        except ValueError:
            return _error("invalid_request", "请求体不是合法的 JSON")
        if not isinstance(payload, dict):
            return _error("invalid_request", "请求体必须是 JSON 对象")
        if draining() and payload.get("cmd") in ("request.get", "request.post", "sessions.create"):
            return _error("busy", "该 worker 正在下线，请重试")
        try:
            status, body = await backend_of(request).call(payload)
        except BackendError as e:
            log.warning("v1 %s rejected [%s]: %s", payload.get("cmd"), e.code, e.message)
            return _error(e.code, e.message)
        return JSONResponse(status_code=status, content=body)

    return app
