"""CNB 云原生开发环境的生命周期推算。

平台回收规则(https://docs.cnb.cool/zh/workspaces/workspace-recycling.html):
  * 最长保持 18 小时;
  * 「环境不过夜」:已运行超过 8 小时且处于凌晨 4~6 点,强制回收。

轮换器与 worker 使用同一套推算:worker 在 drain_at 主动摘除隧道连接,
轮换器在 replace_at 提前拉起替补,在 stop_at 关闭旧环境。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone


def _env_minutes(name: str, default: float) -> timedelta:
    raw = os.getenv(name)
    return timedelta(minutes=float(raw)) if raw and raw.strip() else timedelta(minutes=default)


@dataclass(frozen=True)
class Policy:
    max_run: timedelta = timedelta(hours=18)
    tz: timezone = timezone(timedelta(hours=8))
    night_start: time = time(4, 0)
    night_end: time = time(6, 0)
    night_min_age: timedelta = timedelta(hours=8)
    # 在平台回收前多久开始摘流量。正常情况下替补就绪后旧 worker 会被提前关闭,
    # 这个时刻只在替补迟迟起不来时才生效,所以尽量靠后,让旧 worker 多服务一会儿
    drain_margin: timedelta = timedelta(minutes=10)
    # 摘流量后多久关闭环境(等待进行中的请求结束)
    stop_after_drain: timedelta = timedelta(minutes=3)
    # 在摘流量前多久开始拉起替补。各位置依次交接(每轮最多一个),这段时间要容纳全部位置
    replace_lead: timedelta = timedelta(minutes=70)
    # 离摘流量不足这个时间时,不再等待其他位置交接,立即拉起替补
    urgent_window: timedelta = timedelta(minutes=20)
    # 新环境超过这个时间仍未就绪视为启动失败
    startup_timeout: timedelta = timedelta(minutes=15)

    @classmethod
    def from_env(cls) -> Policy:
        d = cls()
        max_run = d.max_run
        # 环境内由平台注入的真实上限(毫秒)
        if raw := os.getenv("CNB_VSCODE_MAX_RUN_TIME", "").strip():
            max_run = timedelta(milliseconds=int(raw))
        elif raw := os.getenv("FLEET_MAX_RUN_HOURS", "").strip():
            max_run = timedelta(hours=float(raw))
        tz_hours = float(os.getenv("FLEET_TZ_OFFSET_HOURS", "8") or 8)
        return cls(
            max_run=max_run,
            tz=timezone(timedelta(hours=tz_hours)),
            drain_margin=_env_minutes("FLEET_DRAIN_MARGIN_MIN", d.drain_margin.total_seconds() / 60),
            stop_after_drain=_env_minutes("FLEET_STOP_AFTER_DRAIN_MIN", d.stop_after_drain.total_seconds() / 60),
            replace_lead=_env_minutes("FLEET_REPLACE_LEAD_MIN", d.replace_lead.total_seconds() / 60),
            urgent_window=_env_minutes("FLEET_URGENT_MIN", d.urgent_window.total_seconds() / 60),
            startup_timeout=_env_minutes("FLEET_STARTUP_TIMEOUT_MIN", d.startup_timeout.total_seconds() / 60),
        )


@dataclass(frozen=True)
class Timeline:
    start: datetime
    kill_at: datetime
    drain_at: datetime
    stop_at: datetime
    replace_at: datetime


def platform_kill_time(start: datetime, policy: Policy) -> datetime:
    """环境最早会被平台回收的时刻。"""
    hard = start + policy.max_run
    day = start.astimezone(policy.tz).date()
    while True:
        window_start = datetime.combine(day, policy.night_start, policy.tz)
        if window_start >= hard:
            return hard
        window_end = datetime.combine(day, policy.night_end, policy.tz)
        kill = max(window_start, start + policy.night_min_age)
        if kill < window_end and kill < hard:
            return kill
        day += timedelta(days=1)


def timeline(start: datetime, policy: Policy) -> Timeline:
    kill = platform_kill_time(start, policy)
    drain = kill - policy.drain_margin
    return Timeline(
        start=start,
        kill_at=kill,
        drain_at=drain,
        stop_at=min(drain + policy.stop_after_drain, kill),
        replace_at=drain - policy.replace_lead,
    )


def parse_time(value: str) -> datetime:
    """解析 CNB 返回的 ISO 时间,例如 2026-10-05T03:42:38.749Z。"""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
