from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fleet.rotator import Worker, classify, plan
from fleet.schedule import Policy, parse_time, platform_kill_time, timeline

CST = timezone(timedelta(hours=8))
P = Policy()


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=CST)


@pytest.mark.parametrize(
    ("start", "kill"),
    [
        (at(5, 10), at(6, 4)),  # 18 小时上限恰好落在 4 点
        (at(5, 13), at(6, 4)),  # 4 点时已运行 15 小时 → 不过夜规则先触发
        (at(5, 21), at(6, 5)),  # 5 点满 8 小时,仍在 4~6 点窗口内
        (at(5, 23), at(6, 17)),  # 6 点前不满 8 小时 → 跑满 18 小时
        (at(6, 4, 30), at(6, 22, 30)),  # 窗口内新建的环境
        (at(5, 22), at(6, 16)),  # 6 点恰好满 8 小时,窗口为左闭右开
    ],
)
def test_platform_kill_time(start, kill):
    assert platform_kill_time(start, P) == kill


def test_timeline_offsets():
    t = timeline(at(5, 23), P)
    assert t.kill_at == at(6, 17)
    assert t.drain_at == at(6, 16, 50)
    assert t.stop_at == at(6, 16, 53)
    assert t.replace_at == at(6, 15, 40)


def test_policy_reads_platform_max_run(monkeypatch):
    monkeypatch.setenv("CNB_VSCODE_MAX_RUN_TIME", str(12 * 3600 * 1000))
    assert Policy.from_env().max_run == timedelta(hours=12)


def test_parse_time():
    assert parse_time("2026-10-05T03:42:38.749Z") == datetime(2026, 10, 5, 3, 42, 38, 749000, tzinfo=timezone.utc)


def worker(sn: str, created: datetime, state: str = "ready") -> Worker:
    return Worker(sn=sn, created=created, state=state, timeline=timeline(created, P))


NOW = at(5, 12)


def test_empty_fleet_starts_target():
    assert plan([], P, 2, NOW).start == 2


def test_healthy_fleet_is_left_alone():
    p = plan([worker("a", at(5, 9)), worker("b", at(5, 11))], P, 2, NOW)
    assert p.start == 0 and p.stop == []


def test_due_worker_gets_replacement_before_stop():
    due = worker("old", at(4, 18))  # 次日 4 点已运行 10 小时,按不过夜规则 04:00 回收
    fresh = worker("new", at(5, 11))
    now = due.timeline.replace_at + timedelta(minutes=1)
    p = plan([due, fresh], P, 2, now)
    assert p.start == 1
    assert p.stop == []


def test_replacement_starting_counts_as_capacity():
    due = worker("old", at(4, 18))
    fresh = worker("new", at(5, 0))
    now = due.timeline.replace_at + timedelta(minutes=5)
    starting = worker("repl", now - timedelta(minutes=2), state="starting")
    assert plan([due, fresh, starting], P, 2, now).start == 0


def test_drained_worker_is_stopped():
    old = worker("old", at(4, 18))
    now = old.timeline.stop_at
    p = plan([old, worker("b", at(5, 0)), worker("c", now - timedelta(minutes=30))], P, 2, now)
    assert [w.sn for w, _ in p.stop] == ["old"]
    assert p.start == 0


def test_failed_and_stuck_workers_are_replaced():
    failed = worker("f", NOW - timedelta(minutes=5), state="failed")
    stuck = worker("s", NOW - timedelta(minutes=20), state="starting")
    p = plan([failed, stuck], P, 2, NOW)
    assert {w.sn for w, _ in p.stop} == {"f", "s"}
    assert p.start == 2


def test_start_is_capped():
    # 4 个都到了替换时间,但已达上限 target*2,不再创建
    ws = [worker(str(i), at(4, 18, i)) for i in range(4)]
    now = max(w.timeline.replace_at for w in ws) + timedelta(minutes=1)
    assert plan(ws, P, 2, now).start == 0


def status_with(stage_status: str) -> dict:
    return {
        "status": "pending",
        "pipelinesStatus": {
            "cnb-x-001": {
                "stages": [
                    {"name": "Prepare", "status": "success"},
                    {"name": "start-worker", "status": stage_status},
                    {"name": "BeforeEnd", "status": "start"},
                ]
            }
        },
    }


@pytest.mark.parametrize(("stage", "state"), [("success", "ready"), ("start", "starting"), ("error", "failed")])
def test_classify(stage, state):
    ws = {"sn": "cnb-x", "create_time": "2026-10-05T03:42:47.000Z"}
    w = classify(ws, status_with(stage), P)
    assert w.state == state
    assert w.created == datetime(2026, 10, 5, 3, 42, 47, tzinfo=timezone.utc)


def test_classify_without_status_is_starting():
    w = classify({"sn": "cnb-x", "create_time": "2026-10-05T03:42:47.000Z"}, None, P)
    assert w.state == "starting"


# ---------------------------------------------------------------- 多位置(多隧道)
from fleet.agent import tunnel_token_for  # noqa: E402
from fleet.rotator import plan_slots, slot_event, slot_targets  # noqa: E402


def test_slot_targets_and_events():
    assert slot_targets(2, ["a", "b"]) == {"a": 1, "b": 1}
    assert slot_targets(2, ["a"]) == {"a": 2}
    assert slot_targets(3, ["a", "b"]) == {"a": 2, "b": 1}
    assert slot_event("api_trigger_worker", "a", ["a", "b"]) == "api_trigger_worker"
    assert slot_event("api_trigger_worker", "b", ["a", "b"]) == "api_trigger_worker_b"


def test_excess_ready_worker_is_trimmed_oldest_first():
    old, new = worker("old", at(5, 9)), worker("new", at(5, 11))
    p = plan([old, new], P, 1, NOW)
    assert [w.sn for w, _ in p.stop] == ["old"]
    assert p.start == 0


def test_no_trim_while_replacement_starting():
    starting = worker("s", NOW - timedelta(minutes=2), state="starting")
    p = plan([worker("x", at(5, 9)), worker("y", at(5, 11)), starting], P, 1, NOW)
    assert p.stop == [] and p.start == 0


def test_migrate_single_slot_fleet_to_two_slots():
    a1, a2 = worker("a1", at(5, 9)), worker("a2", at(5, 11))
    stops, starts = plan_slots([a1, a2], P, {"a": 1, "b": 1}, NOW)
    assert [w.sn for w, _ in stops] == ["a1"]
    assert starts == ["b"]


def test_slots_are_replaced_independently():
    a = worker("a", at(4, 18))  # 已到替换时间
    b = Worker(sn="b", created=at(5, 11), state="ready", timeline=timeline(at(5, 11), P), slot="b")
    a.slot = "a"
    now = a.timeline.replace_at + timedelta(minutes=1)
    stops, starts = plan_slots([a, b], P, {"a": 1, "b": 1}, now)
    assert stops == [] and starts == ["a"]


def test_tunnel_token_for():
    env = {"TUNNEL_TOKEN": "ta", "TUNNEL_TOKEN_B": "tb"}
    assert tunnel_token_for("a", env) == ("ta", "TUNNEL_TOKEN")
    assert tunnel_token_for("b", env) == ("tb", "TUNNEL_TOKEN_B")
    assert tunnel_token_for("b", {"TUNNEL_TOKEN": "ta"}) == ("ta", "TUNNEL_TOKEN")
    assert tunnel_token_for("b", {}) == ("", "TUNNEL_TOKEN")


def test_due_worker_stopped_once_replacement_ready():
    old = worker("old", at(4, 18))
    now = old.timeline.replace_at + timedelta(minutes=8)
    fresh = worker("fresh", now - timedelta(minutes=5))
    p = plan([old, fresh], P, 1, now)
    assert [(w.sn, r) for w, r in p.stop] == [("old", "替补已就绪")]
    assert p.start == 0


def test_due_worker_kept_while_replacement_starting():
    old = worker("old", at(4, 18))
    now = old.timeline.replace_at + timedelta(minutes=8)
    starting = worker("s", now - timedelta(minutes=1), state="starting")
    p = plan([old, starting], P, 1, now)
    assert p.stop == [] and p.start == 0


def test_trim_prefers_older_worker_when_expiry_ties():
    # 两者都会在次日 04:00 被回收(到期时间相同),应先关创建更早的
    older, newer = worker("older", at(5, 13)), worker("newer", at(5, 13, 40))
    assert older.timeline.replace_at == newer.timeline.replace_at
    p = plan([newer, older], P, 1, at(5, 14))
    assert [w.sn for w, _ in p.stop] == ["older"]



# ---------------------------------------------------------------- 错开轮换
def slotted(sn: str, slot: str, created: datetime, state: str = "ready") -> Worker:
    return Worker(sn=sn, created=created, state=state, timeline=timeline(created, P), slot=slot)


FOUR = {"a": 1, "b": 1, "c": 1, "d": 1}


def test_only_one_slot_starts_rotating_per_run():
    ws = [slotted(s, s, at(4, 18)) for s in "abcd"]
    now = ws[0].timeline.replace_at + timedelta(minutes=1)
    stops, starts = plan_slots(ws, P, FOUR, now)
    assert stops == [] and starts == ["a"]


def test_no_new_rotation_while_a_replacement_is_starting():
    ws = [slotted(s, s, at(4, 18)) for s in "abcd"]
    now = ws[0].timeline.replace_at + timedelta(minutes=11)
    ws.append(slotted("a2", "a", now - timedelta(minutes=1), state="starting"))
    assert plan_slots(ws, P, FOUR, now) == ([], [])


def test_handover_completes_then_next_slot_starts():
    ws = [slotted(s, s, at(4, 18)) for s in "abcd"]
    now = ws[0].timeline.replace_at + timedelta(minutes=11)
    ws.append(slotted("a2", "a", now - timedelta(minutes=9)))  # a 的替补已就绪
    stops, starts = plan_slots(ws, P, FOUR, now)
    assert [w.sn for w, _ in stops] == ["a"]
    assert starts == ["b"]


def test_urgent_slots_start_without_waiting():
    ws = [slotted(s, s, at(4, 18)) for s in "abcd"]
    now = ws[0].timeline.drain_at - timedelta(minutes=15)
    ws.append(slotted("a2", "a", now - timedelta(minutes=1), state="starting"))
    _, starts = plan_slots(ws, P, FOUR, now)
    assert sorted(starts) == ["b", "c", "d"]


def test_recovery_is_not_throttled_by_rotations():
    ws = [slotted(s, s, at(4, 18)) for s in "bcd"]  # 位置 a 空缺
    now = ws[0].timeline.replace_at + timedelta(minutes=1)
    _, starts = plan_slots(ws, P, FOUR, now)
    assert starts == ["a", "b"]


def test_staggered_rotation_fits_before_drain():
    # 按每 10 分钟一轮模拟:替补启动后 1 分钟就绪,4 个位置都应在摘流量前完成交接
    ws = [slotted(s, s, at(4, 18)) for s in "abcd"]
    drain = ws[0].timeline.drain_at
    now = ws[0].timeline.replace_at + timedelta(minutes=3)
    started: dict[str, datetime] = {}
    while now < drain:
        for w in ws:
            if w.state == "starting" and now - w.created >= timedelta(minutes=1):
                w.state = "ready"
        stops, starts = plan_slots(ws, P, FOUR, now)
        gone = {w.sn for w, _ in stops}
        ws = [w for w in ws if w.sn not in gone]
        for slot in starts:
            started.setdefault(slot, now)
            ws.append(slotted(f"{slot}-new", slot, now, state="starting"))
        now += timedelta(minutes=10)
    assert sorted(started) == ["a", "b", "c", "d"]
    times = sorted(started.values())
    assert all(later - earlier >= timedelta(minutes=10) for earlier, later in zip(times, times[1:]))
    assert {w.sn for w in ws} == {"a-new", "b-new", "c-new", "d-new"}


# ---------------------------------------------------------------- 版本滚动更新与账号上限
from fleet.rotator import title_version  # noqa: E402


def test_title_version():
    assert title_version("worker b v=2 10-05 15:40 #1") == "2"
    assert title_version("rollout async+stagger") is None
    assert title_version(None) is None


def test_outdated_workers_roll_one_slot_at_a_time():
    ws = [slotted(s, s, at(5, 9)) for s in "abcd"]
    for w in ws:
        w.outdated = True
    stops, starts = plan_slots(ws, P, FOUR, at(5, 12))
    assert stops == [] and starts == ["a"]


def test_outdated_worker_stopped_once_current_replacement_ready():
    old = slotted("old", "a", at(5, 9))
    old.outdated = True
    new = slotted("new", "a", at(5, 11, 55))
    stops, starts = plan_slots([old, new], P, {"a": 1}, at(5, 12))
    assert [(w.sn, r) for w, r in stops] == [("old", "替补已就绪(旧版本)")]
    assert starts == []


def test_capacity_limits_starts_and_recovery_comes_first():
    ws = [slotted(s, s, at(4, 18)) for s in "bcd"]  # a 空缺,b/c/d 都已紧急
    now = ws[0].timeline.drain_at - timedelta(minutes=15)
    _, starts = plan_slots(ws, P, FOUR, now, capacity=2)
    assert starts == ["a", "b"]
    _, starts = plan_slots(ws, P, FOUR, now, capacity=0)
    assert starts == []


# ---------------------------------------------------------------- 自动扩缩容
from fleet.rotator import autoscale, fleet_load  # noqa: E402


def _fleet(*workers):
    return {"slots": {chr(97 + i): w for i, w in enumerate(workers)}}


def _stats(active=0, queued=0, minutes=()):
    return {"health": {"capacity": 5, "active": active, "queued": queued}, "minutes": [list(m) for m in minutes]}


def test_fleet_load_uses_littles_law_and_ignores_offline():
    now = 10_000.0
    m = int(now // 60) * 60
    load = fleet_load(_fleet(_stats(2, 1, [(m, 6, 0, 120.0)]), _stats(0, 0, [(m, 0, 1, 0.0)]), {"error": "HTTP 530"}), now)
    assert load["capacity"] == 5 and load["now"] == 3
    assert load["peak10"] == (120 + 60) / 60  # 两台同一分钟合计 3 个并发
    old = fleet_load(_fleet(_stats(minutes=[(m - 20 * 60, 30, 0, 600.0)])), now)
    assert old["peak10"] == 0 and old["peak30"] == 10


def test_autoscale_decisions():
    idle = {"capacity": 5, "now": 0, "peak10": 0.0, "peak30": 0.0}
    assert autoscale(idle, 1, 1, 4, True)[0] == 1
    # 1 台满载(5 并发)> 3.5:扩到 2 台;排队很多时一次扩到位,但不超过上限
    assert autoscale({**idle, "now": 5}, 1, 1, 4, True)[0] == 2
    assert autoscale({**idle, "now": 30}, 1, 1, 4, True)[0] == 4
    # 30 分钟峰值低于少一台后容量的 35% 才缩容,且每轮只少一台
    assert autoscale({**idle, "peak30": 1.0}, 3, 1, 4, True)[0] == 2
    assert autoscale({**idle, "peak30": 4.0}, 3, 1, 4, True)[0] == 3
    assert autoscale(idle, 3, 1, 4, False)[0] == 3  # 刚扩容不缩
    assert autoscale(idle, 1, 2, 4, True)[0] == 2  # 不低于下限


def test_drain_closes_tunnel_only_after_worker_is_idle(monkeypatch):
    import types

    import fleet.agent as agent_mod

    class Tunnel:
        alive = True
        term_sent_at = None

        def terminate(self):
            self.term_sent_at = 1.0

    clock = {"t": 100.0}
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: clock["t"])
    a = types.SimpleNamespace(tunnel=Tunnel(), drain_started=100.0, idle_since=None)
    step = lambda health: agent_mod.Agent.drain_step(a, health)  # noqa: E731
    step({"active": 2, "queued": 0, "tasks_pending": 2})
    clock["t"] = 110.0
    step({"active": 0, "queued": 0, "tasks_pending": 0})  # 刚空闲:再留 10 秒给客户端取结果
    assert a.tunnel.term_sent_at is None
    clock["t"] = 121.0
    step({"active": 0, "queued": 0, "tasks_pending": 0})
    assert a.tunnel.term_sent_at is not None
    # 一直忙:最多等 DRAIN_MAX 秒
    b = types.SimpleNamespace(tunnel=Tunnel(), drain_started=100.0, idle_since=None)
    clock["t"] = 100.0 + agent_mod.DRAIN_MAX + 1
    agent_mod.Agent.drain_step(b, {"active": 3, "queued": 1, "tasks_pending": 4})
    assert b.tunnel.term_sent_at is not None


def test_tunnel_watchdog_restarts_cloudflared_after_stall(monkeypatch):
    import types

    import fleet.agent as agent_mod

    class Proc:
        def kill(self):
            pass

        def wait(self, timeout):
            return 0

    class Tunnel:
        alive = True
        proc = Proc()
        starts = 0

        def start(self):
            self.starts += 1

    clock = {"t": 0.0}
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: clock["t"])
    a = types.SimpleNamespace(tunnel=Tunnel(), draining=False, tunnel_down_since=None, tunnel_kicked_at=float("-inf"), tunnel_restarts=0)
    tick = lambda ok, t: (clock.update(t=t), agent_mod.Agent.tunnel_watchdog(a, ok))  # noqa: E731
    tick(True, 0)
    tick(False, 5)  # 刚断开:开始计时
    tick(False, 20)
    assert a.tunnel.starts == 0
    tick(False, 26)  # 断开超过 20 秒:重启
    assert a.tunnel.starts == 1 and a.tunnel_restarts == 1
    tick(False, 60)  # 60 秒内最多重启一次
    assert a.tunnel.starts == 1
    tick(False, 87)
    assert a.tunnel.starts == 2
    tick(True, 90)  # 连上后重新计时
    tick(False, 95)
    tick(False, 120)
    assert a.tunnel.starts == 2 and a.tunnel_down_since == 95
    a.draining = True  # 摘流量时隧道由 drain_step 负责
    tick(False, 300)
    assert a.tunnel.starts == 2
