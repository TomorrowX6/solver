from __future__ import annotations

import types

import fleet.agent as agent_mod
from fleet.agent import task_prefix, tunnel_token_for


def test_tunnel_token_for():
    env = {"TUNNEL_TOKEN": "ta", "TUNNEL_TOKEN_B": "tb"}
    assert tunnel_token_for("a", env) == ("ta", "TUNNEL_TOKEN")
    assert tunnel_token_for("b", env) == ("tb", "TUNNEL_TOKEN_B")
    assert tunnel_token_for("b", {"TUNNEL_TOKEN": "ta"}) == ("ta", "TUNNEL_TOKEN")
    assert tunnel_token_for("b", {}) == ("", "TUNNEL_TOKEN")


def test_task_prefix_starts_with_slot_index():
    assert task_prefix("a", "w1")[0] == "0" and task_prefix("e", "w1")[0] == "4" and task_prefix("p", "w1")[0] == "f"
    assert len(task_prefix("g", "w1")) == 8 and task_prefix("g", "w1") != task_prefix("g", "w2")


def test_drain_closes_tunnel_only_after_worker_is_idle(monkeypatch):
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


def test_tunnel_protocol_reads_last_registered_connection(tmp_path):
    from fleet.agent import tunnel_protocol

    log = tmp_path / "tunnel.log"
    assert tunnel_protocol(log) is None  # 还没有日志
    log.write_text(
        "2026-10-08T05:00:00Z INF Initial protocol quic\n"
        "2026-10-08T05:00:01Z INF Registered tunnel connection connIndex=0 connection=x event=0 ip=198.41.192.7 location=lax01 protocol=quic\n"
        "2026-10-08T05:03:00Z INF Switching to fallback protocol http2\n"
        "2026-10-08T05:03:01Z INF Registered tunnel connection connIndex=0 connection=y event=0 ip=198.41.200.13 location=lax09 protocol=http2\n",
        encoding="utf-8",
    )
    assert tunnel_protocol(log) == "http2"


def test_tunnel_drops_are_counted_once_per_outage():
    a = types.SimpleNamespace(tunnel=object(), draining=False, tunnel_drops=0, tunnel_was_ok=False)
    for ok in (False, True, True, False, False, True, False):  # 启动时还没连上不算掉线
        agent_mod.Agent.track_tunnel(a, ok)
    assert a.tunnel_drops == 2
    a.draining = True  # 摘流量时主动断开隧道,不算掉线
    agent_mod.Agent.track_tunnel(a, True)
    agent_mod.Agent.track_tunnel(a, False)
    assert a.tunnel_drops == 2
