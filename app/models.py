from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl

# 这些字段会被写进注入页面的 JS 中,限制字符集
_SAFE = r"^[A-Za-z0-9_\-]+$"


class SolveRequest(BaseModel):
    url: HttpUrl = Field(description="部署 Turnstile 的页面地址;组件会在这个域名下渲染,域名需与 sitekey 绑定的域名一致")
    sitekey: str = Field(min_length=1, max_length=128, pattern=_SAFE)
    action: str | None = Field(default=None, max_length=32, pattern=_SAFE)
    cdata: str | None = Field(default=None, max_length=255, pattern=_SAFE)
    proxy: str | None = Field(default=None, max_length=512, description="http://user:pass@host:port 或 socks5://host:port")
    timeout: float | None = Field(default=None, ge=5, description="总超时(秒),缺省使用服务端默认值")


class SolveResponse(BaseModel):
    token: str
    elapsed: float
    attempts: int
    user_agent: str | None = None


class Health(BaseModel):
    status: str
    worker: str | None = None
    backend: str
    backend_version: str | None = None
    solver: str
    # 求解器不可用时的原因(初始化失败等)
    solver_error: str | None = None
    active: int
    capacity: int
    queued: int
    completed: int
    failed: int
    solved: int
    solve_failed: int
    rejected: int
    sessions: int
    tasks_pending: int = 0
    # 正在下线:不再接受新任务
    draining: bool = False
    # /solve 与任务每次尝试失败的原因计数(含随后重试成功的)
    attempt_errors: dict[str, int] = {}
    # 拿到 token 时已点击的次数与 token 来源(callback / getResponse / input)的计数
    token_stats: dict[str, int] = {}
    # 仅 Linux 提供,来自容器 cgroup:累计 CPU 秒数(两次采样之差 / 间隔 / cpu_limit = 利用率)、核数上限、已用内存
    cpu_seconds: float | None = None
    cpu_limit: float | None = None
    mem_used_mb: int | None = None
