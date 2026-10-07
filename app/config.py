from __future__ import annotations

import os
from dataclasses import dataclass


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw and raw.strip() else default


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    # 设置后 /solve 要求 X-API-Key 或 Authorization: Bearer,任务接口要求 clientKey;对外暴露时必须设置
    api_key: str = ""

    # FlareSolverr 地址(只用于健康检查)
    backend_url: str = "http://127.0.0.1:8191"

    # 同时运行的求解浏览器数(/solve 与异步任务共用)
    max_concurrency: int = 4
    # 槽位全满时最多允许多少个请求排队,超出直接返回 429;0 表示不限
    max_queue: int = 0
    # 求解总超时上限(秒):异步任务用它,/solve 的 timeout 不超过它。部署在 Cloudflare 后面时应小于其 100 秒的源站超时
    max_timeout: float = 85.0

    # /solve:单次求解的默认总超时、每次尝试的超时(超出即换新浏览器重试)
    default_timeout: float = 60.0
    attempt_timeout: float = 35.0
    # 承载组件的页面:auto = 先试同域名 /robots.txt 再用调用方的 url;light / full 只用其中一个
    solve_page: str = "auto"
    # 组件注入方式:write = document.open/write 重写整份文档;innerhtml = 替换 documentElement 的内容
    solve_inject: str = "write"
    # 点击方式:mouse(默认)/ keyboard / alternate。实测键盘方式(Tab + 空格)在注入页面上基本拿不到 token
    click_mode: str = "mouse"
    # 设置后把组件截图保存到该目录(点击前、点击后、放弃时),用于排查
    debug_dir: str = ""
    # 追加给求解浏览器的启动参数(空格分隔);参数文件存在时每次启动浏览器都重新读取
    chrome_args: str = ""
    chrome_args_file: str = ""
    # FlareSolverr 源码目录,/solve 复用其中的反检测浏览器
    flaresolverr_dir: str = "/app"

    # createTask / getTaskResult:taskId 前缀(多副本部署时为「位置编号 + worker 标识」,8 位以内十六进制)、
    # 结果保留时间(秒)、最多同时未完成的任务数
    task_prefix: str = ""
    task_ttl: float = 300.0
    max_pending_tasks: int = 200

    # 在 /health 中返回,便于区分请求落在哪个副本
    worker_id: str = ""
    # 守护进程的状态文件(阶段、摘流量与回收时间、版本),在 /admin/stats 中返回;为空或不存在时不返回
    agent_state_file: str = ""
    # 下线标记文件:存在时不再接受新任务(返回繁忙,由上游改投其他 worker),只继续返回已有任务的结果
    drain_file: str = ""
    # 组件在复选框出现前多少秒没有新事件判定为卡住。并发高时挑战计算变慢,8 秒会误判并引发连锁重试
    # (实测 4 核、并发 10:8 秒时 40 个任务重试 88 次、14 个失败;20 秒时 0 次重试)
    stall_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> Settings:
        d = cls()
        return cls(
            host=_env_str("TS_HOST", d.host),
            port=_env_int("TS_PORT", d.port),
            api_key=_env_str("TS_API_KEY", d.api_key),
            backend_url=_env_str("TS_BACKEND_URL", d.backend_url),
            max_concurrency=_env_int("TS_MAX_CONCURRENCY", d.max_concurrency),
            max_queue=_env_int("TS_MAX_QUEUE", d.max_queue),
            max_timeout=_env_float("TS_MAX_TIMEOUT", d.max_timeout),
            default_timeout=_env_float("TS_DEFAULT_TIMEOUT", d.default_timeout),
            attempt_timeout=_env_float("TS_ATTEMPT_TIMEOUT", d.attempt_timeout),
            solve_page=_env_str("TS_SOLVE_PAGE", d.solve_page),
            solve_inject=_env_str("TS_SOLVE_INJECT", d.solve_inject),
            click_mode=_env_str("TS_CLICK_MODE", d.click_mode),
            debug_dir=_env_str("TS_DEBUG_DIR", d.debug_dir),
            chrome_args=_env_str("TS_CHROME_ARGS", d.chrome_args),
            chrome_args_file=_env_str("TS_CHROME_ARGS_FILE", d.chrome_args_file),
            flaresolverr_dir=_env_str("TS_FLARESOLVERR_DIR", d.flaresolverr_dir),
            task_prefix=_env_str("TS_TASK_PREFIX", d.task_prefix),
            task_ttl=_env_float("TS_TASK_TTL", d.task_ttl),
            max_pending_tasks=_env_int("TS_MAX_PENDING_TASKS", d.max_pending_tasks),
            worker_id=_env_str("TS_WORKER_ID", d.worker_id),
            agent_state_file=_env_str("TS_AGENT_STATE_FILE", d.agent_state_file),
            drain_file=_env_str("TS_DRAIN_FILE", d.drain_file),
            stall_seconds=_env_float("TS_STALL_SECONDS", d.stall_seconds),
        )
