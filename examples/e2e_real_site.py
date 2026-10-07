"""对真实 Turnstile 站点做端到端测试:求解拿到 token → 交给站点后端用 siteverify 校验(并核对 hostname 与 action)。

    SOLVER_URL=https://solver.000.moe TS_API_KEY=... python examples/e2e_real_site.py

环境变量:
    SOLVER_URL   网关地址(流水线内默认取 FLEET_PUBLIC_URL)
    TS_API_KEY   网关的 API Key
    E2E_MODE     task(默认,createTask + 每 3 秒 getTaskResult)或 solve(POST /solve 同步)
    SITE_URL     测试站点,默认 https://turnstile-test.000.moe(提供 /config 与 /verify,见 testsite/)
    E2E_ROUNDS   每轮并发数,逗号分隔,默认 1,8,24
    E2E_REPEAT   每个并发档位跑几批,默认 1

只用标准库;自定义了 User-Agent(urllib 默认 UA 会被 Cloudflare 拦截)。
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

UA = "turnstile-e2e/1.0"


def request(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: float = 120):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": UA, "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw[:200].decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001 - 网络错误计入结果
        return type(e).__name__, {}


def one_run(solver: str, key: str, site: str, cfg: dict, mode: str) -> dict:
    t0 = time.monotonic()
    if mode == "task":
        task = {"type": "TurnstileTaskProxyless", "websiteURL": f"{site}/", "websiteKey": cfg["sitekey"],
                "metadata": {"action": cfg["action"]}}
        code, created = request("POST", f"{solver}/createTask", {"clientKey": key, "task": task})
        solved: dict = created
        if code == 200 and created.get("errorId") == 0:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                time.sleep(3)
                code, solved = request("POST", f"{solver}/getTaskResult", {"clientKey": key, "taskId": created["taskId"]})
                if code != 200 or solved.get("status") != "processing":
                    break
        token = (solved.get("solution") or {}).get("token") if solved.get("status") == "ready" else None
        if not token:
            solved = {"code": solved.get("errorCode") or solved.get("status"), "message": solved.get("errorDescription")}
    else:
        code, solved = request(
            "POST",
            f"{solver}/solve",
            {"url": f"{site}/", "sitekey": cfg["sitekey"], "action": cfg["action"], "timeout": 80},
            {"X-API-Key": key},
        )
        token = solved.get("token") if code == 200 else None
    solve_s = time.monotonic() - t0
    if not token:
        reason = solved.get("code") or solved.get("message") or solved
        return {"stage": "solve", "code": code, "error": str(reason)[:120], "solve_s": solve_s}
    vcode, verdict = request("POST", f"{site}/verify", {"token": token})
    sv = verdict.get("siteverify", {})
    return {
        "stage": "verify",
        "ok": vcode == 200 and verdict.get("ok") is True,
        "solve_s": solve_s,
        "attempts": solved.get("attempts", 1),
        "errors": sv.get("error-codes") or ([] if verdict.get("ok") else [verdict.get("error") or "mismatch"]),
        "hostname": sv.get("hostname"),
        "action": sv.get("action"),
    }


def sample_health(solver: str, stop: threading.Event, samples: list) -> None:
    while not stop.is_set():
        code, d = request("GET", f"{solver}/health", timeout=15)
        if code == 200 and d.get("cpu_seconds") is not None:
            samples.append((time.time(), d["worker"], d["cpu_seconds"], d["cpu_limit"], d["mem_used_mb"], d["active"]))
        time.sleep(0.5)


def cpu_report(samples: list) -> list[str]:
    lines = []
    for worker in sorted({s[1] for s in samples}):
        ws = [s for s in samples if s[1] == worker]
        if len(ws) < 2 or ws[-1][0] - ws[0][0] < 2:
            continue
        util = (ws[-1][2] - ws[0][2]) / (ws[-1][0] - ws[0][0]) / ws[0][3] * 100
        lines.append(
            f"    {worker}: CPU 平均 {util:.0f}%(共 {ws[0][3]:g} 核),内存峰值 {max(s[4] for s in ws)}MB,"
            f"同时在解峰值 {max(s[5] for s in ws)}"
        )
    return lines


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    solver = (os.getenv("SOLVER_URL") or os.getenv("FLEET_PUBLIC_URL") or "").rstrip("/")
    key = os.getenv("TS_API_KEY", "")
    site = os.getenv("SITE_URL", "https://turnstile-test.000.moe").rstrip("/")
    rounds = [int(x) for x in os.getenv("E2E_ROUNDS", "1,8,24").split(",")]
    mode = os.getenv("E2E_MODE", "task")
    repeat = int(os.getenv("E2E_REPEAT", "1"))
    if not solver or not key:
        print("缺少 SOLVER_URL 或 TS_API_KEY", file=sys.stderr)
        return 2

    for _ in range(3):  # 国内到 Cloudflare 偶有连接被重置
        code, cfg = request("GET", f"{site}/config")
        if code == 200 and cfg.get("sitekey"):
            break
        time.sleep(3)
    if code != 200 or not cfg.get("sitekey"):
        print(f"测试站点未就绪: {code} {cfg}", file=sys.stderr)
        return 2
    print(f"站点 {site}  sitekey={cfg['sitekey']}  action={cfg['action']}  solver={solver}  模式={mode}")

    failed_total = 0
    for n in rounds:
        for batch in range(repeat):
            samples: list = []
            stop = threading.Event()
            sampler = threading.Thread(target=sample_health, args=(solver, stop, samples))
            sampler.start()
            t0 = time.monotonic()
            with ThreadPoolExecutor(n) as pool:
                results = list(pool.map(lambda _: one_run(solver, key, site, cfg, mode), range(n)))
            wall = time.monotonic() - t0
            stop.set()
            sampler.join()

            ok = [r for r in results if r.get("ok")]
            failed_total += n - len(ok)
            times = sorted(r["solve_s"] for r in ok) or [0.0]
            p90 = times[min(len(times) - 1, int(len(times) * 0.9))]
            print(
                f"\n并发 {n}(第 {batch + 1} 批):通过 {len(ok)}/{n},总耗时 {wall:.1f}s,吞吐 {len(ok) / wall:.2f}/s,"
                f"求解耗时 中位 {statistics.median(times):.1f}s / P90 {p90:.1f}s / 最慢 {times[-1]:.1f}s,"
                f"重试 {sum(r.get('attempts', 1) - 1 for r in ok)} 次"
            )
            fails = Counter(
                f"solve {r['code']} {r['error']}" if r["stage"] == "solve" else f"verify {','.join(map(str, r['errors']))}"
                for r in results
                if not r.get("ok")
            )
            for reason, count in fails.most_common():
                print(f"    失败 {count} 次:{reason}")
            for line in cpu_report(samples):
                print(line)
            time.sleep(3)

    return 1 if failed_total else 0


if __name__ == "__main__":
    sys.exit(main())
