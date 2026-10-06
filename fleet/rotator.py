"""Worker 轮换器:由 CNB 定时任务周期执行,每次根据 API 返回的现状做一轮对齐。

    python -m fleet.rotator status              # 查看各 worker 生命周期
    python -m fleet.rotator reconcile [--dry-run]

环境变量:
    CNB_API_TOKEN   访问令牌(需 repo-cnb-trigger:rw、repo-cnb-detail:r、account-engage:rw)
    CNB_REPO        仓库路径,流水线内默认取 CNB_REPO_SLUG
    FLEET_TARGET    常驻 worker 总数,默认 2,平均分到各位置
    FLEET_MIN / FLEET_MAX  自动扩缩容的范围(默认都等于 FLEET_TARGET,即不扩缩)。按负载在范围内调整 worker 数,
                    负载取自 FLEET_PUBLIC_URL/api/fleet(需要 TS_API_KEY)
    FLEET_SLOTS     位置列表,默认 a。每个位置对应一条 Cloudflare 隧道,例如 a,b
    FLEET_VERSION   期望的 worker 版本,默认 1。写进 worker 的构建标题;版本不符的 worker 按错开规则逐个替换
    FLEET_MAX_WORKSPACES  账号同时运行的开发环境上限(含其他仓库),默认 6
    FLEET_BRANCH    worker 所在分支,默认 main
    FLEET_EVENT     位置 a 的 api_trigger 事件名,默认 api_trigger_worker;其他位置为 <事件名>_<位置>
    FLEET_PUBLIC_URL  可选,对外地址;运行时顺带探测 /health
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .cnb import CnbClient, CnbError
from .schedule import Policy, Timeline, parse_time, timeline

READY_STAGE = "start-worker"
_FAILED = {"error", "fail", "failed", "cancel", "cancelled", "canceled", "timeout", "skipped"}
_VERSION_RE = re.compile(r"\bv=(\S+)")


@dataclass
class Worker:
    sn: str
    created: datetime
    state: str  # starting | ready | failed
    timeline: Timeline
    detail: str = ""
    slot: str = "a"
    # 版本与期望不符:视同到期,替补就绪后关闭
    outdated: bool = False


@dataclass
class Plan:
    stop: list[tuple[Worker, str]] = field(default_factory=list)
    start: int = 0


def slot_event(base_event: str, slot: str, slots: list[str]) -> str:
    return base_event if slot == slots[0] else f"{base_event}_{slot}"


def slot_targets(target: int, slots: list[str]) -> dict[str, int]:
    base, extra = divmod(target, len(slots))
    return {s: base + (1 if i < extra else 0) for i, s in enumerate(slots)}


def title_version(title: str | None) -> str | None:
    m = _VERSION_RE.search(title or "")
    return m.group(1) if m else None


def classify(
    workspace: dict, build_status: dict | None, policy: Policy, slot: str = "a", outdated: bool = False
) -> Worker:
    created = parse_time(workspace["create_time"])
    state, detail = "starting", ""
    pipelines = (build_status or {}).get("pipelinesStatus") or {}
    for pipeline in pipelines.values():
        for stage in pipeline.get("stages") or []:
            if stage.get("name") != READY_STAGE:
                continue
            status = str(stage.get("status", "")).lower()
            detail = status
            if status == "success":
                state = "ready"
            elif status in _FAILED:
                state = "failed"
    return Worker(
        sn=workspace["sn"],
        created=created,
        state=state,
        timeline=timeline(created, policy),
        detail=detail,
        slot=slot,
        outdated=outdated,
    )


def _is_due(w: Worker, now: datetime) -> bool:
    return w.state == "ready" and (w.outdated or now >= w.timeline.replace_at)


def _is_healthy(w: Worker, now: datetime) -> bool:
    return w.state == "ready" and not _is_due(w, now)


def plan(workers: list[Worker], policy: Policy, target: int, now: datetime) -> Plan:
    """对同一位置的 worker 做规划。"""
    result = Plan()
    for w in workers:
        if w.state == "failed":
            result.stop.append((w, f"启动阶段失败({w.detail})"))
        elif w.state == "starting" and now - w.created > policy.startup_timeout:
            result.stop.append((w, "启动超时"))
        elif now >= w.timeline.stop_at:
            result.stop.append((w, "已摘流量,到期关闭"))

    stopping = {w.sn for w, _ in result.stop}
    remaining = [w for w in workers if w.sn not in stopping]
    # 未到替换时间、版本也对的就绪 worker 与正在启动的 worker 才算未来容量
    healthy = [w for w in remaining if _is_healthy(w, now)]
    starting = [w for w in remaining if w.state == "starting"]
    due = [w for w in remaining if _is_due(w, now)]

    # 替补已就绪:立即关闭到期(或版本落后)的旧 worker,关闭前 endStages 会先优雅摘除隧道连接
    if due and len(healthy) >= target:
        result.stop.extend((w, "替补已就绪(旧版本)" if w.outdated else "替补已就绪") for w in due)

    # 多出来的就绪 worker(例如手动多起了一台)关掉最早到期的;有 worker 正在启动时先不动
    if not starting and len(healthy) > target:
        for w in sorted(healthy, key=lambda w: (w.timeline.replace_at, w.created))[: len(healthy) - target]:
            result.stop.append((w, "超出目标数量"))
        return result

    need = target - len(healthy) - len(starting)
    # 上限防止 API 异常时无限创建
    room = target * 2 - len(remaining)
    result.start = max(0, min(need, room))
    return result


def plan_slots(
    workers: list[Worker],
    policy: Policy,
    targets: dict[str, int],
    now: datetime,
    capacity: int | None = None,
) -> tuple[list[tuple[Worker, str]], list[str]]:
    """各位置分别规划,再错开轮换:同一时间最多只有一个位置在交接。

    补位(位置空缺、启动失败等)优先;因到期或版本落后而拉起的替补每轮最多一个,
    且有替补正在启动时不再新开,除非某个位置离摘流量已不足 urgent_window。
    capacity 为本轮最多还能新建的环境数(账号并发上限减去关闭后仍在运行的数量)。
    """
    stops: list[tuple[Worker, str]] = []
    recovery: list[str] = []
    rotations: list[tuple[datetime, str, int]] = []
    for slot, target in targets.items():
        mine = [w for w in workers if w.slot == slot]
        p = plan(mine, policy, target, now)
        stops.extend(p.stop)
        stopping = {w.sn for w, _ in p.stop}
        alive = [w for w in mine if w.sn not in stopping]
        healthy = sum(1 for w in alive if _is_healthy(w, now))
        starting = sum(1 for w in alive if w.state == "starting")
        due = [w for w in alive if _is_due(w, now)]
        # 到期的旧 worker 仍在服务,这部分缺口属于轮换;超出的才是真正的空缺
        n_recovery = min(p.start, max(0, target - healthy - starting - len(due)))
        recovery.extend([slot] * n_recovery)
        if p.start > n_recovery:
            rotations.append((min(w.timeline.drain_at for w in due), slot, p.start - n_recovery))

    # 不在当前位置列表里的 worker(例如缩减了位置)按到期规则处理,不再补充
    for w in workers:
        if w.slot not in targets and now >= w.timeline.stop_at:
            stops.append((w, "已摘流量,到期关闭"))

    starts = list(recovery)
    stopping = {w.sn for w, _ in stops}
    may_start_one = not any(w.state == "starting" and w.sn not in stopping for w in workers)
    for drain_at, slot, count in sorted(rotations):
        if drain_at - now <= policy.urgent_window:
            starts.extend([slot] * count)
        elif may_start_one:
            starts.extend([slot] * count)
            may_start_one = False

    if capacity is not None:
        starts = starts[: max(0, capacity)]
    return stops, starts


def load_workers(
    client: CnbClient,
    branch: str,
    policy: Policy,
    event_to_slot: dict[str, str],
    default_slot: str,
    version: str,
) -> list[Worker]:
    workers = []
    for ws in client.list_running_workspaces(branch=branch):
        sn = ws["sn"]
        try:
            status = client.build_status(sn)
        except CnbError as e:
            print(f"[warn] 查询 {sn} 构建状态失败: {e}", file=sys.stderr)
            status = None
        try:
            info = client.build_info(sn)
        except CnbError as e:
            print(f"[warn] 查询 {sn} 构建记录失败: {e}", file=sys.stderr)
            info = None
        event = (info or {}).get("event")
        if event is not None and event not in event_to_slot:
            # 自检等其他事件创建的环境不归轮换器管理
            continue
        # 查询失败时不判定为旧版本,避免误替换
        outdated = info is not None and title_version(info.get("title")) != version
        workers.append(classify(ws, status, policy, event_to_slot.get(event or "", default_slot), outdated))
    return sorted(workers, key=lambda w: (w.slot, w.created))


# ------------------------------------------------------------------ 自动扩缩容
SCALE_UP_AT = 0.7  # 负载超过总容量的 70% 扩容
SCALE_DOWN_AT = 0.35  # 少一台后 30 分钟内的峰值仍低于 35% 才缩容(每轮最多少一台)


def fleet_load(fleet: dict, now: float) -> dict:
    """由各 worker 的 /admin/stats 估算负载(并发数)。

    当前负载 = 正在求解 + 排队;历史负载按 Little 定律由每分钟的忙碌秒数估算(成功按实际耗时,失败按 60 秒),
    取最近 10 / 30 分钟中最高的一分钟。
    """
    live = [s for s in (fleet.get("slots") or {}).values() if s and not s.get("error") and s.get("health")]
    busy: dict[int, float] = {}
    for s in live:
        for m, _ok, fail, elapsed in s.get("minutes") or []:
            busy[m] = busy.get(m, 0.0) + elapsed + fail * 60
    def peak(minutes: int) -> float:
        return max((v / 60 for m, v in busy.items() if m >= now - minutes * 60), default=0.0)
    return {
        "capacity": max((s["health"].get("capacity") or 0 for s in live), default=0),
        "now": sum((s["health"].get("active") or 0) + (s["health"].get("queued") or 0) for s in live),
        "peak10": peak(10),
        "peak30": peak(30),
    }


def autoscale(load: dict, current: int, min_n: int, max_n: int, can_shrink: bool, default_capacity: int = 5) -> tuple[int, str]:
    """返回 (目标 worker 数, 原因)。"""
    cap = load.get("capacity") or default_capacity
    demand = max(load["now"], load["peak10"])
    need = math.ceil(demand / (cap * SCALE_UP_AT)) if demand > 0 else 0
    clamp = lambda n: max(min_n, min(max_n, n))  # noqa: E731
    if need > current:
        return clamp(need), f"扩容:负载 {demand:.1f},{current} 台容量 {current * cap}"
    recent = max(load["now"], load["peak30"])
    if can_shrink and current > min_n and recent <= (current - 1) * cap * SCALE_DOWN_AT:
        return clamp(current - 1), f"缩容:30 分钟峰值负载 {recent:.1f}"
    return clamp(current), f"保持:负载 {demand:.1f}"


def fetch_fleet(public_url: str, api_key: str) -> dict | None:
    req = urllib.request.Request(
        public_url.rstrip("/") + "/api/fleet",
        headers={"User-Agent": "turnstile-rotator/1.0", "X-API-Key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 获取负载失败: {e}", file=sys.stderr)
        return None


def _fmt(dt: datetime, policy: Policy) -> str:
    return dt.astimezone(policy.tz).strftime("%m-%d %H:%M")


def print_status(workers: list[Worker], policy: Policy, now: datetime) -> None:
    print(f"now {_fmt(now, policy)}  running={len(workers)}")
    print(f"{'sn':<22}{'slot':<6}{'state':<10}{'ver':<5}{'age':>7}  {'replace':<12}{'drain':<12}{'kill':<12}")
    for w in workers:
        age = (now - w.created).total_seconds() / 3600
        t = w.timeline
        print(
            f"{w.sn:<22}{w.slot:<6}{w.state:<10}{'old' if w.outdated else 'ok':<5}{age:>6.1f}h  "
            f"{_fmt(t.replace_at, policy):<12}{_fmt(t.drain_at, policy):<12}{_fmt(t.kill_at, policy):<12}"
        )


def probe_public(url: str) -> str:
    # Python-urllib 的默认 UA 会被 Cloudflare 浏览器完整性检查拦截(error 1010)
    req = urllib.request.Request(url.rstrip("/") + "/health", headers={"User-Agent": "turnstile-rotator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode()[:300]
    except Exception as e:  # noqa: BLE001 - 仅用于展示
        return f"不可用: {e}"


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m fleet.rotator")
    p.add_argument("command", choices=["status", "reconcile"])
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    token = os.getenv("CNB_API_TOKEN", "").strip()
    repo = (os.getenv("CNB_REPO") or os.getenv("CNB_REPO_SLUG") or "").strip()
    if not token or not repo:
        print("缺少 CNB_API_TOKEN 或 CNB_REPO", file=sys.stderr)
        return 2
    target = int(os.getenv("FLEET_TARGET", "2"))
    slots = [s.strip().lower() for s in os.getenv("FLEET_SLOTS", "a").split(",") if s.strip()]
    version = os.getenv("FLEET_VERSION", "1").strip()
    max_workspaces = int(os.getenv("FLEET_MAX_WORKSPACES", "6"))
    branch = os.getenv("FLEET_BRANCH", "main")
    base_event = os.getenv("FLEET_EVENT", "api_trigger_worker")
    events = {slot: slot_event(base_event, slot, slots) for slot in slots}

    policy = Policy.from_env()
    client = CnbClient(token, repo)
    now = datetime.now(timezone.utc)
    workers = load_workers(client, branch, policy, {e: s for s, e in events.items()}, slots[0], version)
    running_total = len(client.list_running_workspaces(all_repos=True))
    print_status(workers, policy, now)
    print(f"version={version}  account running={running_total}/{max_workspaces}")

    public = os.getenv("FLEET_PUBLIC_URL", "").strip()
    if public:
        print(f"public {public}: {probe_public(public)}")

    min_n = int(os.getenv("FLEET_MIN", str(target)))
    max_n = int(os.getenv("FLEET_MAX", str(target)))
    if min_n < max_n:
        # 当前在服务的位置数:有就绪或正在启动的 worker 的位置
        in_service = {w.slot for w in workers if w.state in ("ready", "starting") and now < w.timeline.stop_at}
        current = len(in_service) or min_n
        # 刚扩容(15 分钟内有新 worker)或正在启动时不缩容,避免来回切换
        can_shrink = not any(w.state == "starting" or (now - w.created).total_seconds() < 900 for w in workers)
        fleet = fetch_fleet(public, os.getenv("TS_API_KEY", "")) if public else None
        if fleet:
            # 只按本轮换器管理的位置计算(自有服务器渠道不归它扩缩)
            fleet = {**fleet, "slots": {k: v for k, v in (fleet.get("slots") or {}).items() if k in slots}}
            load = fleet_load(fleet, now.timestamp())
            target, reason = autoscale(load, current, min_n, max_n, can_shrink)
        else:
            target, reason = max(min_n, min(max_n, current)), "保持:无法获取负载"
        print(f"autoscale [{min_n}-{max_n}]: {reason} -> {target} 台")
    if args.command == "status":
        return 0

    stops, _ = plan_slots(workers, policy, slot_targets(target, slots), now)
    capacity = max_workspaces - (running_total - len(stops))
    stops, starts = plan_slots(workers, policy, slot_targets(target, slots), now, capacity=capacity)
    if not stops and not starts:
        print("无需操作")
        return 0

    errors = 0
    # 先关后开:要关闭的旧 worker 都已摘流量或替补已就绪,先腾出账号的并发名额
    for w, reason in stops:
        if args.dry_run:
            print(f"[dry-run] 关闭 {w.sn} [{w.slot}]: {reason}")
            continue
        try:
            client.stop_workspace(w.sn)
            print(f"关闭 {w.sn} [{w.slot}]: {reason}")
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[error] 关闭 {w.sn} 失败: {e}", file=sys.stderr)

    if starts and stops and not args.dry_run:
        # 关闭需要几秒到一分钟(endStages 摘流量);等名额真正释放再创建,否则会被平台以超出并发上限拒绝
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            running_total = len(client.list_running_workspaces(all_repos=True))
            if running_total + len(starts) <= max_workspaces:
                break
            time.sleep(5)
        starts = starts[: max(0, max_workspaces - running_total)]

    for i, slot in enumerate(starts):
        title = f"worker {slot} v={version} {_fmt(now, policy)} #{i + 1}"
        if args.dry_run:
            print(f"[dry-run] 创建 {title}({events[slot]})")
            continue
        try:
            res = client.start_build(branch, events[slot], title, env={"FLEET_VERSION": version})
            print(f"创建 worker [{slot}]: {json.dumps(res, ensure_ascii=False)}")
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[error] 创建 worker [{slot}] 失败: {e}", file=sys.stderr)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
