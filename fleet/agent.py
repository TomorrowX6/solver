"""Worker 守护进程:在云原生开发环境内拉起 FlareSolverr、网关、cloudflared,并在生命周期末尾主动摘流量。

    python -m fleet.agent run     # 守护运行(start-worker.sh 以后台方式启动)
    python -m fleet.agent wait    # 阻塞到就绪,作为 start-worker 阶段的成败依据
    python -m fleet.agent drain   # 立即摘流量并等待完成(endStages 使用)

需要的环境变量(来自密钥仓库):TUNNEL_TOKEN(位置 b 为 TUNNEL_TOKEN_B)、TS_API_KEY。
FLEET_SLOT 指定 worker 所在位置,默认 a。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .schedule import Policy, parse_time, timeline

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.getenv("AGENT_STATE_DIR", "/tmp/turnstile-agent"))
STATE_FILE = STATE_DIR / "state.json"
DRAIN_FILE = STATE_DIR / "drain"
PORT = int(os.getenv("TS_PORT", "8686"))
METRICS = os.getenv("TUNNEL_METRICS", "127.0.0.1:20241")
TUNNEL_GRACE = int(os.getenv("TUNNEL_GRACE_SECONDS", "60"))
# 摘流量时先停止接收新任务,等进行中的任务结束(最多 DRAIN_MAX 秒)并留 DRAIN_TAIL 秒给客户端取结果,再断开隧道
DRAIN_MAX = int(os.getenv("AGENT_DRAIN_MAX_SECONDS", "120"))
DRAIN_TAIL = 10
# 看门狗:隧道断开(没有任何连接)超过 TUNNEL_STALL 秒就重启 cloudflared,每 TUNNEL_KICK_INTERVAL 秒最多一次。
# 出口 NAT 重置时所有连接同时断开,cloudflared 按指数退避重连要几分钟,新进程则立即重连
TUNNEL_STALL = int(os.getenv("AGENT_TUNNEL_STALL_SECONDS", "20"))
TUNNEL_KICK_INTERVAL = 60
TUNNEL_ENABLED = os.getenv("AGENT_DISABLE_TUNNEL", "") not in {"1", "true", "yes"}
# 常驻模式(自有服务器渠道):没有平台回收,不按生命周期摘流量
PERMANENT = os.getenv("AGENT_PERMANENT", "") in {"1", "true", "yes"}
# FlareSolverr 源码目录与端口(上游镜像中位于 /app)
FLARESOLVERR_DIR = Path(os.getenv("FLARESOLVERR_DIR", "/app"))
FLARESOLVERR_PORT = int(os.getenv("FLARESOLVERR_PORT", "8191"))
# 自检:经网关用 Cloudflare 官方测试 sitekey(任何域名都会通过)真实求解一次,确认浏览器与出口网络都正常;
# 承载页面需要能注入脚本(没有严格 CSP)
SOLVE_TEST_URL = os.getenv("AGENT_SOLVE_TEST_URL", "https://example.com/")
SOLVE_TEST_SITEKEY = "1x00000000000000000000AA"
SLOT_ORDER = "abcdefghijklmnop"


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [agent] {msg}", flush=True)


def tunnel_token_for(slot: str, env: dict[str, str]) -> tuple[str, str]:
    """返回 (令牌, 来源变量名)。位置 a 用 TUNNEL_TOKEN,其他位置用 TUNNEL_TOKEN_<位置>,未配置时退回 TUNNEL_TOKEN。"""
    name = "TUNNEL_TOKEN" if slot == "a" else f"TUNNEL_TOKEN_{slot.upper()}"
    if token := env.get(name, "").strip():
        return token, name
    return env.get("TUNNEL_TOKEN", "").strip(), "TUNNEL_TOKEN"


def http_ok(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def task_prefix(slot: str, worker_id: str) -> str:
    index = SLOT_ORDER.index(slot) if slot in SLOT_ORDER else 0
    return f"{index:x}" + hashlib.sha1(worker_id.encode()).hexdigest()[:7]


def tunnel_protocol(log_path: Path) -> str | None:
    """cloudflared 最近一次注册连接用的协议(quic / http2)。--protocol auto 时 QUIC 连不上会退回 http2。"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = re.findall(r"Registered tunnel connection.*?protocol=(\w+)", text)
    return found[-1] if found else None


def http_json(url: str, timeout: float = 5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None


class Child:
    def __init__(self, name: str, argv: list[str], env: dict[str, str] | None = None, cwd: Path = ROOT) -> None:
        self.name = name
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.restarts: deque[float] = deque()
        self.term_sent_at: float | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        logfile = open(STATE_DIR / f"{self.name}.log", "ab")
        self.proc = subprocess.Popen(
            self.argv,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logfile.close()
        log(f"started {self.name} pid={self.proc.pid}")

    def restart_budget_left(self) -> bool:
        now = time.monotonic()
        while self.restarts and now - self.restarts[0] > 600:
            self.restarts.popleft()
        return len(self.restarts) < 5

    def terminate(self) -> None:
        if self.alive and self.term_sent_at is None:
            assert self.proc is not None
            self.proc.terminate()
            self.term_sent_at = time.monotonic()

    def stop(self, timeout: float) -> None:
        if not self.alive:
            return
        assert self.proc is not None
        self.proc.terminate()
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Agent:
    def __init__(self) -> None:
        self.policy = Policy.from_env()
        started = os.getenv("CNB_BUILD_START_TIME")
        self.timeline = timeline(parse_time(started) if started else datetime.now(timezone.utc), self.policy)
        # CNB 上是构建号;自有服务器用主机名加启动时间,重启后旧任务号不会被误认
        self.worker_id = os.getenv("CNB_BUILD_ID") or os.getenv("AGENT_WORKER_ID") or f"{socket.gethostname()}-{int(time.time())}"
        self.slot = os.getenv("FLEET_SLOT", "a").strip().lower() or "a"
        self.tunnel_token, self.tunnel_source = tunnel_token_for(self.slot, dict(os.environ))
        # 不接隧道(自检)时服务不对外,没有配置就随机生成一个,只供自检使用
        self.api_key = os.getenv("TS_API_KEY", "") or ("" if TUNNEL_ENABLED else secrets.token_urlsafe(24))
        self.phase = "starting"
        self.error = ""
        self.ever_ready = False
        self.self_test_ok = False
        self.next_self_test = 0.0
        self.draining = False
        self.drain_reason = ""
        self.drain_started = 0.0
        self.idle_since: float | None = None
        self.stop_requested = False
        self.tunnel_down_since: float | None = None
        self.tunnel_kicked_at = float("-inf")
        self.tunnel_restarts = 0
        # 隧道从有连接变成一个连接都没有的次数,用来比较不同隧道协议的稳定性
        self.tunnel_drops = 0
        self.tunnel_was_ok = False

        # 业务进程都不需要隧道令牌
        base_env = {k: v for k, v in os.environ.items() if not k.startswith("TUNNEL_TOKEN")}
        flaresolverr_env = {
            **base_env,
            "HOST": "127.0.0.1",
            "PORT": str(FLARESOLVERR_PORT),
            # HEADLESS=true 时 FlareSolverr 自己在 Xvfb 虚拟显示里运行有界面的 Chromium
            "HEADLESS": os.getenv("HEADLESS", "true"),
            "LOG_LEVEL": os.getenv("LOG_LEVEL", "info"),
        }
        gateway_env = {
            **base_env,
            "TS_HOST": "0.0.0.0",
            "TS_PORT": str(PORT),
            "TS_BACKEND_URL": f"http://127.0.0.1:{FLARESOLVERR_PORT}",
            "TS_WORKER_ID": f"{self.worker_id}/{self.slot}",
            "TS_API_KEY": self.api_key,
            # taskId 第一段:位置编号(Cloudflare Worker 据此路由查询)+ worker 标识(识别轮换前的旧任务)
            "TS_TASK_PREFIX": task_prefix(self.slot, self.worker_id),
            # 网关在 /admin/stats 中返回守护进程的状态(阶段、摘流量与回收时间、版本)
            "TS_AGENT_STATE_FILE": str(STATE_FILE),
            # 摘流量标记:存在时网关不再接受新任务
            "TS_DRAIN_FILE": str(DRAIN_FILE),
        }

        self.flaresolverr = Child(
            "flaresolverr", [sys.executable, "-u", str(FLARESOLVERR_DIR / "flaresolverr.py")], flaresolverr_env,
            cwd=FLARESOLVERR_DIR,
        )
        self.gateway = Child("gateway", [sys.executable, "run.py"], gateway_env)
        self.children: list[Child] = [self.flaresolverr, self.gateway]
        self.tunnel: Child | None = None
        if TUNNEL_ENABLED:
            proto = os.getenv("TUNNEL_PROTOCOL", "http2")
            tunnel_env = {k: v for k, v in os.environ.items() if not k.startswith("TUNNEL_TOKEN")}
            tunnel_env["TUNNEL_TOKEN"] = self.tunnel_token
            self.tunnel = Child(
                "tunnel",
                [
                    "cloudflared", "tunnel", "--no-autoupdate", "--protocol", proto,
                    "--grace-period", f"{TUNNEL_GRACE}s", "--metrics", METRICS, "run",
                ],
                tunnel_env,
            )
            self.children.append(self.tunnel)

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        if not self.api_key:
            self.fail("TS_API_KEY 未设置:服务经隧道暴露在公网,必须开启鉴权")
        if TUNNEL_ENABLED and not self.tunnel_token:
            self.fail("TUNNEL_TOKEN 未设置")
        elif TUNNEL_ENABLED and self.slot != "a" and self.tunnel_source == "TUNNEL_TOKEN":
            log(f"WARNING: 未配置 TUNNEL_TOKEN_{self.slot.upper()},位置 {self.slot} 暂时接入隧道 A")
        if self.phase == "failed":
            self.write_state()
            return 1

        # 容器重启后 /tmp 仍在:清掉上次留下的摘流量标记,否则网关一启动就拒绝新任务
        DRAIN_FILE.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        t = self.timeline
        log(
            f"worker={self.worker_id} slot={self.slot} tunnel={self.tunnel_source if TUNNEL_ENABLED else 'disabled'} "
            + ("permanent" if PERMANENT else f"drain_at={t.drain_at.isoformat()} kill_at={t.kill_at.isoformat()}")
        )

        # FlareSolverr 启动自检时 undetected-chromedriver 会改写 /app/chromedriver;
        # 网关预热也会启动浏览器,同时改写会损坏驱动,所以等 FlareSolverr 健康后再启动其余进程
        self.flaresolverr.start()
        ready_by = time.monotonic() + 120
        while time.monotonic() < ready_by and not self.stop_requested:
            if (http_json(f"http://127.0.0.1:{FLARESOLVERR_PORT}/health") or {}).get("status") == "ok":
                log("flaresolverr is healthy")
                break
            time.sleep(1)
        for child in self.children:
            if child is not self.flaresolverr:
                child.start()

        while not self.stop_requested:
            self.tick()
            self.write_state()
            time.sleep(2)

        log("shutting down")
        if self.tunnel:
            self.tunnel.stop(TUNNEL_GRACE + 10)
        for child in reversed(self.children):
            child.stop(10)
        return 0

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        if not self.draining:
            if not PERMANENT and now >= self.timeline.drain_at:
                self.begin_drain("到达生命周期末尾")
            elif DRAIN_FILE.exists():
                self.begin_drain("收到摘流量请求")

        for child in self.children:
            if child.alive or (child is self.tunnel and self.draining):
                continue
            code = child.proc.returncode if child.proc else None
            if not child.restart_budget_left():
                self.fail(f"{child.name} 10 分钟内反复退出(最后退出码 {code}),放弃重启")
                continue
            log(f"{child.name} exited with {code}, restarting")
            child.restarts.append(time.monotonic())
            child.start()

        if self.tunnel and self.tunnel.term_sent_at is not None and self.tunnel.alive:
            if time.monotonic() - self.tunnel.term_sent_at > TUNNEL_GRACE + 15:
                log("tunnel did not exit in time, killing")
                self.tunnel.proc.kill()  # type: ignore[union-attr]

        health = http_json(f"http://127.0.0.1:{PORT}/health") or {}
        solver_ok = health.get("status") == "ok"  # 网关在线且 FlareSolverr 健康
        if self.draining:
            self.drain_step(health)
        tunnel_ok = self.tunnel is None or http_ok(f"http://{METRICS}/ready")
        self.tunnel_watchdog(tunnel_ok)
        self.track_tunnel(tunnel_ok)
        if solver_ok and not self.self_test_ok and time.monotonic() >= self.next_self_test:
            self.self_test_ok = self.self_test()
            self.next_self_test = time.monotonic() + 20

        if self.phase == "failed":
            return
        if self.draining:
            self.phase = "draining" if self.tunnel and self.tunnel.alive else "drained"
        elif solver_ok and tunnel_ok and self.self_test_ok:
            if not self.ever_ready:
                log("worker ready")
            self.phase, self.ever_ready = "ready", True
        else:
            self.phase = "degraded" if self.ever_ready else "starting"

    def track_tunnel(self, tunnel_ok: bool) -> None:
        if not self.tunnel or self.draining:
            return
        if self.tunnel_was_ok and not tunnel_ok:
            self.tunnel_drops += 1
            log(f"tunnel lost all connections (drop #{self.tunnel_drops})")
        self.tunnel_was_ok = tunnel_ok

    def tunnel_watchdog(self, tunnel_ok: bool) -> None:
        """隧道断开超过 TUNNEL_STALL 秒时重启 cloudflared;不计入进程反复退出的次数。"""
        tunnel = self.tunnel
        if not tunnel or self.draining or not tunnel.alive or tunnel_ok:
            self.tunnel_down_since = None
            return
        now = time.monotonic()
        if self.tunnel_down_since is None:
            self.tunnel_down_since = now
            return
        if now - self.tunnel_down_since < TUNNEL_STALL or now - self.tunnel_kicked_at < TUNNEL_KICK_INTERVAL:
            return
        log(f"tunnel disconnected for {now - self.tunnel_down_since:.0f}s, restarting cloudflared")
        assert tunnel.proc is not None
        tunnel.proc.kill()  # 没有连接,也就没有进行中的请求,不必等 grace period
        try:
            tunnel.proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
        tunnel.start()
        self.tunnel_kicked_at = self.tunnel_down_since = now
        self.tunnel_restarts += 1

    def _post(self, path: str, body: dict, timeout: float = 70) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": self.api_key},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def self_test(self) -> bool:
        """/solve 用测试 sitekey 拿到 token 才算就绪。"""
        try:
            solved = self._post("/solve", {"url": SOLVE_TEST_URL, "sitekey": SOLVE_TEST_SITEKEY, "timeout": 45})
            if not solved.get("token"):
                log(f"self-test /solve failed: {solved}")
                return False
            log(f"self-test passed: /solve token={solved['token'][:20]} in {solved.get('elapsed')}s")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"self-test failed: {e}")
            return False

    def begin_drain(self, reason: str) -> None:
        self.draining, self.drain_reason = True, reason
        self.drain_started = time.monotonic()
        DRAIN_FILE.touch()  # 网关看到标记后不再接受新任务,上游改投其他 worker
        log(f"draining: {reason}")

    def drain_step(self, health: dict) -> None:
        """进行中与排队的任务都结束后再断开隧道,避免客户端取不到结果。"""
        if not (self.tunnel and self.tunnel.alive and self.tunnel.term_sent_at is None):
            return
        now = time.monotonic()
        busy = (health.get("active") or 0) + (health.get("queued") or 0) + (health.get("tasks_pending") or 0)
        if busy or not health:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        idle_long_enough = self.idle_since is not None and now - self.idle_since >= DRAIN_TAIL
        if idle_long_enough or now - self.drain_started >= DRAIN_MAX:
            log("drain: worker idle, closing tunnel" if idle_long_enough else "drain: timeout, closing tunnel")
            # cloudflared 收到 SIGTERM 后从边缘摘除连接,并在 grace period 内等待进行中的请求完成
            self.tunnel.terminate()

    def fail(self, message: str) -> None:
        if self.phase != "failed":
            log(f"FAILED: {message}")
        self.phase, self.error = "failed", message

    def _on_signal(self, signum, _frame) -> None:
        self.stop_requested = True

    def write_state(self) -> None:
        state = {
            "phase": self.phase,
            "error": self.error,
            "worker": self.worker_id,
            "slot": self.slot,
            "tunnel_token": self.tunnel_source if TUNNEL_ENABLED else None,
            "pid": os.getpid(),
            "drain_reason": self.drain_reason,
            "tunnel_restarts": self.tunnel_restarts,
            "tunnel_drops": self.tunnel_drops,
            "tunnel_protocol": tunnel_protocol(STATE_DIR / "tunnel.log") if self.tunnel else None,
            # 版本由轮换器创建 worker 时传入;commit 为构建所用的提交
            "fleet_version": os.getenv("FLEET_VERSION") or None,
            "commit": (os.getenv("CNB_COMMIT") or "")[:7] or None,
            "started_at": self.timeline.start.isoformat(),
            "drain_at": None if PERMANENT else self.timeline.drain_at.isoformat(),
            "kill_at": None if PERMANENT else self.timeline.kill_at.isoformat(),
            "children": {c.name: c.alive for c in self.children},
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wait_for(phases: set[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state = read_state()
        phase = state.get("phase")
        if phase != last:
            log(f"phase: {phase}")
            last = phase
        if phase in phases or phase == "failed":
            return state
        time.sleep(2)
    return read_state()


def tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    command = sys.argv[1] if len(sys.argv) > 1 else "run"

    if command == "run":
        DRAIN_FILE.unlink(missing_ok=True)
        return Agent().run()

    if command == "wait":
        state = wait_for({"ready"}, float(os.getenv("AGENT_WAIT_TIMEOUT", "480")))
        if state.get("phase") == "ready":
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        print(f"worker 未就绪: {json.dumps(state, ensure_ascii=False)}", file=sys.stderr)
        for name in ("gateway", "flaresolverr", "tunnel"):
            if text := tail(STATE_DIR / f"{name}.log"):
                print(f"----- {name}.log -----\n{text}", file=sys.stderr)
        return 1

    if command == "drain":
        DRAIN_FILE.touch()
        state = wait_for({"drained"}, DRAIN_MAX + TUNNEL_GRACE + 30)
        print(json.dumps(state, ensure_ascii=False))
        return 0 if state.get("phase") == "drained" else 1

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
