from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.backend import BackendError, FlareSolverr, Slots
from app.config import Settings
from app.main import create_app
from app.tasks import TaskError, new_task_id, parse_task
from app.stats import SolveLog
from app.turnstile import SolveFailed, SolveResult, TurnstileSolver, candidate_pages

KEY = "s3cret"
AUTH = {"X-API-Key": KEY}


def sk(name: str) -> str:
    """格式合法的 sitekey;最后一段决定模拟求解器的行为。"""
    return f"0x4AAAAAAAAAAAAAAAAAAA_{name}"


class FakeFlareSolverr:
    """模拟 FlareSolverr 的 /health 与首页(网关只用它们做健康检查)。"""

    def __init__(self) -> None:
        self.down = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"msg": "FlareSolverr is ready!", "version": "3.5.2", "userAgent": "UA"})


@pytest.fixture
def fake() -> FakeFlareSolverr:
    return FakeFlareSolverr()


class FakeTurnstileSolver:
    """替代真实浏览器:sitekey 的最后一段(见 sk())决定行为。"""

    def __init__(self, slots, max_workers, flaresolverr_dir, page_mode="auto", inject="write", click_mode="mouse", debug_dir="", chrome_args="", chrome_args_file="", stall_seconds=20.0) -> None:
        self.slots = slots
        self.available = True
        self.solved = 0
        self.failed = 0
        self.calls: list[dict] = []
        self.history = SolveLog()

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def solve(self, url, sitekey, action, cdata, timeout, attempt_timeout, admit_check=True):
        self.calls.append({"url": url, "sitekey": sitekey, "action": action, "cdata": cdata, "timeout": timeout})
        name = sitekey.rsplit("_", 1)[-1]
        errors = {"badkey": "turnstile_error", "slow": "timeout", "full": "busy", "down": "solver_unavailable"}
        if name in errors:
            raise BackendError(errors[name], sitekey)
        async with self.slots.hold(check=admit_check):
            if name == "wait":
                await asyncio.sleep(0.3)
            self.solved += 1
            self.history.record(url, sitekey, True, 1.0, 1)
            return SolveResult(token=f"tok-{sitekey}", elapsed=1.0, user_agent="UA", attempts=1, page=url)


@pytest.fixture(autouse=True)
def fake_solver(monkeypatch):
    monkeypatch.setattr(main_module, "TurnstileSolver", FakeTurnstileSolver)


@pytest.fixture
def make_client(fake):
    def factory(**overrides) -> TestClient:
        settings = Settings(**{"api_key": KEY, **overrides})
        return TestClient(create_app(settings, transport=httpx.MockTransport(fake.handler)))

    return factory


def async_app(fake: FakeFlareSolverr, **overrides):
    """不经 lifespan,直接挂上 backend 与求解器,便于用 ASGITransport 并发请求。"""
    settings = Settings(**{"api_key": KEY, **overrides})
    app = create_app(settings)
    app.state.backend = FlareSolverr(settings, transport=httpx.MockTransport(fake.handler))
    app.state.solver = FakeTurnstileSolver(Slots(settings.max_concurrency, settings.max_queue), 1, "/app")
    return app


SOLVE = {"url": "https://example.com/login", "sitekey": sk("ok"), "action": "login"}


def test_auth(make_client):
    with make_client() as c:
        r = c.post("/solve", json=SOLVE)
        assert r.status_code == 401
        assert r.json() == {"status": "error", "message": "缺少或错误的 API Key", "code": "unauthorized"}
        assert c.post("/solve", json=SOLVE, headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.post("/solve", json=SOLVE, headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
        assert c.get("/health").status_code == 200


def test_flaresolverr_interface_is_gone(make_client):
    # 5 秒盾(/v1)已下线
    with make_client() as c:
        assert c.post("/v1", json={"cmd": "request.get", "url": "https://example.com/"}, headers=AUTH).status_code == 404


def test_backend_unavailable(make_client, fake):
    with make_client() as c:
        fake.down = True
        h = c.get("/health").json()
        assert h["status"] == "degraded" and h["backend"] == "down"


def test_health_reports_backend_and_counters(make_client):
    with make_client(worker_id="w/a", max_concurrency=6) as c:
        c.post("/solve", json=SOLVE, headers=AUTH)
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["backend"] == "ok" and h["backend_version"] == "3.5.2"
        assert h["worker"] == "w/a" and h["capacity"] == 6 and h["solved"] == 1
        assert c.get("/").json()["version"] == "3.5.2"


def test_busy_when_slots_and_queue_full(fake):
    app = async_app(fake, max_concurrency=1, max_queue=1)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            body = {**SOLVE, "sitekey": sk("wait")}
            return await asyncio.gather(*(client.post("/solve", json=body, headers=AUTH) for _ in range(3)))

    responses = asyncio.run(run())
    assert sorted(r.status_code for r in responses) == [200, 200, 429]
    busy = next(r for r in responses if r.status_code == 429)
    assert busy.json()["code"] == "busy" and busy.headers["retry-after"] == "2"


# ---------------------------------------------------------------- /solve
def test_solve_returns_token(make_client):
    with make_client(default_timeout=40) as c:
        r = c.post("/solve", json=SOLVE, headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"token": f"tok-{sk('ok')}", "elapsed": 1.0, "attempts": 1, "user_agent": "UA"}
        call = c.app.state.solver.calls[-1]
        assert call["url"] == "https://example.com/login" and call["action"] == "login" and call["timeout"] == 40
        assert c.get("/health").json()["solved"] == 1


def test_solve_requires_key_and_valid_input(make_client):
    with make_client() as c:
        assert c.post("/solve", json=SOLVE).status_code == 401
        for bad in ({"sitekey": "x'</script>"}, {"url": "not-a-url"}, {"action": "a" * 33}, {"timeout": 1}):
            assert c.post("/solve", json={**SOLVE, **bad}, headers=AUTH).status_code == 422
        # 不是 Turnstile sitekey(例如填成了 API 令牌):不启动浏览器
        r = c.post("/solve", json={**SOLVE, "sitekey": "sk-C9hPQNaKnabcdefghijklmnop"}, headers=AUTH)
        assert r.status_code == 422 and "sitekey 格式不对" in r.text and not c.app.state.solver.calls


def test_solve_rejects_proxy(make_client):
    with make_client() as c:
        r = c.post("/solve", json={**SOLVE, "proxy": "http://u:p@1.2.3.4:8080"}, headers=AUTH)
        assert r.status_code == 422 and "不支持 proxy" in r.text
        assert not c.app.state.solver.calls
        assert c.post("/solve", json={**SOLVE, "proxy": None}, headers=AUTH).status_code == 200


def test_solve_timeout_is_capped(make_client):
    with make_client(max_timeout=85) as c:
        c.post("/solve", json={**SOLVE, "timeout": 300}, headers=AUTH)
        assert c.app.state.solver.calls[-1]["timeout"] == 85


@pytest.mark.parametrize(
    ("sitekey", "status", "code"),
    [("badkey", 422, "turnstile_error"), ("slow", 500, "timeout"), ("full", 429, "busy"), ("down", 503, "solver_unavailable")],
)
def test_solve_error_mapping(make_client, sitekey, status, code):
    with make_client() as c:
        r = c.post("/solve", json={**SOLVE, "sitekey": sk(sitekey)}, headers=AUTH)
        assert r.status_code == status
        assert r.json()["code"] == code and r.json()["status"] == "error"


class ScriptedSolver(TurnstileSolver):
    """真实的重试逻辑 + 预设的每次尝试结果。"""

    def __init__(self, outcomes, attempt_seconds=0.0):
        super().__init__(Slots(2, 0), 2, "/nonexistent")
        self._utils = object()  # 视为可用
        self.outcomes = list(outcomes)
        self.budgets: list[float] = []
        self.attempt_seconds = attempt_seconds

    def _solve_blocking(self, url, config, budget):
        self.budgets.append(budget)
        time.sleep(self.attempt_seconds)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, "page"


def run_solve(solver, timeout=60, attempt_timeout=35):
    async def go():
        try:
            return await solver.solve("https://example.com/", "0xKEY", None, None, timeout, attempt_timeout)
        finally:
            await solver.stop()

    return asyncio.run(go())


def test_solver_retries_with_fresh_browser_after_timeout():
    solver = ScriptedSolver([SolveFailed("timeout", "t"), "tok"])
    result = run_solve(solver)
    assert result.token == "tok" and result.attempts == 2
    assert solver.budgets[0] == 35  # 单次尝试受 attempt_timeout 限制


def test_solver_does_not_retry_config_errors():
    solver = ScriptedSolver([SolveFailed("turnstile_error", "110200"), "tok"])
    with pytest.raises(BackendError) as e:
        run_solve(solver)
    assert e.value.code == "turnstile_error" and len(solver.budgets) == 1


def test_solver_stops_retrying_when_budget_is_short():
    # 总超时 16 秒:第一次尝试耗时 1.5 秒后剩余不足 15 秒,不再换浏览器重试
    solver = ScriptedSolver([SolveFailed("timeout", "t"), "tok"], attempt_seconds=1.5)
    with pytest.raises(BackendError) as e:
        run_solve(solver, timeout=16)
    assert e.value.code == "timeout" and len(solver.budgets) == 1 and solver.budgets[0] <= 16


def test_candidate_pages():
    assert candidate_pages("https://a.com/login?x=1", "auto") == ["https://a.com/robots.txt", "https://a.com/login?x=1"]
    assert candidate_pages("https://a.com/robots.txt", "auto") == ["https://a.com/robots.txt"]
    assert candidate_pages("https://a.com/x", "full") == ["https://a.com/x"]


# ---------------------------------------------------------------- createTask / getTaskResult
import re  # noqa: E402

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TASK = {"type": "TurnstileTaskProxyless", "websiteURL": "https://react-turnstile.vercel.app", "websiteKey": sk("wait")}


def poll(c, task_id, key=KEY, rounds=50):
    for _ in range(rounds):
        r = c.post("/getTaskResult", json={"clientKey": key, "taskId": task_id}).json()
        if r.get("status") != "processing":
            return r
        time.sleep(0.05)
    return r


def test_create_and_get_task_result(make_client):
    with make_client(task_prefix="1abcdef0") as c:
        r = c.post("/createTask", json={"clientKey": KEY, "task": TASK})
        assert r.status_code == 200
        body = r.json()
        assert body["errorId"] == 0 and body["errorCode"] == "" and UUID_RE.match(body["taskId"])
        assert body["taskId"].startswith("1abcdef0")
        first = c.post("/getTaskResult", json={"clientKey": KEY, "taskId": body["taskId"]}).json()
        assert first == {"errorId": 0, "errorCode": None, "errorDescription": None, "status": "processing"}
        done = poll(c, body["taskId"])
        assert done["errorId"] == 0 and done["status"] == "ready"
        assert done["solution"] == {"token": f"tok-{sk('wait')}", "userAgent": "UA"}
        assert c.get("/health").json()["tasks_pending"] == 0


def test_task_metadata_is_passed(make_client):
    task = {
        "type": "TurnstileTaskProxyless", "websiteURL": "https://a.com/", "websiteKey": sk("k1"),
        "metadata": {"action": "login", "cdata": "abc"},
    }
    with make_client() as c:
        task_id = c.post("/createTask", json={"clientKey": KEY, "task": task}).json()["taskId"]
        poll(c, task_id)
        call = c.app.state.solver.calls[-1]
        assert call["action"] == "login" and call["cdata"] == "abc"


@pytest.mark.parametrize(
    ("task", "code"),
    [
        ({**TASK, "type": "RecaptchaV2Task"}, "ERROR_TASK_NOT_SUPPORTED"),
        # websiteKey 不是 Turnstile sitekey(API 令牌、截断的值)
        ({**TASK, "websiteKey": "sk-C9hPQNaKnabcdefghijklmnop"}, "ERROR_INVALID_TASK_DATA"),
        ({**TASK, "websiteKey": "0x4AAA"}, "ERROR_INVALID_TASK_DATA"),
        ({"type": "TurnstileTaskProxyless", "websiteURL": "https://a.com"}, "ERROR_INVALID_TASK_DATA"),
        ({**TASK, "websiteURL": "a.com"}, "ERROR_INVALID_TASK_DATA"),
        # 不支持经调用方的代理求解
        ({**TASK, "type": "TurnstileTask", "proxy": "http:1.2.3.4:8080:u:p"}, "ERROR_TASK_NOT_SUPPORTED"),
        ({**TASK, "type": "AntiTurnstileTask", "proxyType": "http", "proxyAddress": "h", "proxyPort": 8080}, "ERROR_TASK_NOT_SUPPORTED"),
        (None, "ERROR_INVALID_TASK_DATA"),
    ],
)
def test_create_task_validation(make_client, task, code):
    with make_client() as c:
        body = c.post("/createTask", json={"clientKey": KEY, "task": task}).json()
        assert body["errorId"] == 1 and body["errorCode"] == code


def test_client_key_checked(make_client):
    with make_client() as c:
        for path, extra in (("/createTask", {"task": TASK}), ("/getTaskResult", {"taskId": "x"}), ("/getBalance", {})):
            body = c.post(path, json={"clientKey": "wrong", **extra}).json()
            assert body["errorId"] == 1 and body["errorCode"] == "ERROR_KEY_DOES_NOT_EXIST"
        assert c.post("/getBalance", json={"clientKey": KEY}).json()["balance"] > 0


def test_task_ids_from_other_workers_are_invalid(make_client):
    with make_client(task_prefix="2aaaaaaa") as c:
        for task_id in ("2bbbbbbb-0000-0000-0000-000000000000", "2aaaaaaa-0000-0000-0000-000000000000", ""):
            body = c.post("/getTaskResult", json={"clientKey": KEY, "taskId": task_id}).json()
            assert body["errorId"] == 1 and body["errorCode"] == "ERROR_TASKID_INVALID"


def test_failed_task_reports_unsolvable(make_client):
    with make_client() as c:
        task_id = c.post("/createTask", json={"clientKey": KEY, "task": {**TASK, "websiteKey": sk("slow")}}).json()["taskId"]
        body = poll(c, task_id)
        assert body["errorId"] == 1 and body["errorCode"] == "ERROR_CAPTCHA_UNSOLVABLE"


def test_no_slot_available_when_full(make_client):
    with make_client(max_concurrency=1, max_queue=1) as c:
        codes = [c.post("/createTask", json={"clientKey": KEY, "task": TASK}).json() for _ in range(3)]
        assert [b["errorId"] for b in codes[:2]] == [0, 0]
        assert codes[2]["errorCode"] == "ERROR_NO_SLOT_AVAILABLE"
        # 已接收的任务不会因为名额紧张而失败
        assert all(poll(c, b["taskId"])["status"] == "ready" for b in codes[:2])


def test_task_parsing_helpers():
    assert new_task_id("3abc")[:4] == "3abc" and UUID_RE.match(new_task_id("3abc"))
    assert UUID_RE.match(new_task_id(""))
    p = parse_task({"type": "antiturnstiletaskproxyless", "websiteURL": "https://a.com", "websiteKey": sk("k"), "action": "x", "data": "y"})
    assert (p.action, p.cdata) == ("x", "y")
    with pytest.raises(TaskError) as e:
        parse_task({"type": "TurnstileTask", "websiteURL": "https://a.com", "websiteKey": sk("k"), "proxy": "h:1"})
    assert e.value.code == "ERROR_TASK_NOT_SUPPORTED" and "TurnstileTaskProxyless" in e.value.description


def test_widget_page_embeds_config_safely():
    from app.turnstile import widget_page

    html = widget_page({"sitekey": "0xK", "cData": "x</script>"})
    assert "__CONFIG__" not in html and '"sitekey": "0xK"' in html
    assert "x</script>" not in html and r"x<\/script>" in html


def test_solver_retries_after_browser_exception():
    solver = ScriptedSolver([RuntimeError("chrome not reachable"), "tok"])
    result = run_solve(solver)
    assert result.token == "tok" and result.attempts == 2


def test_attempt_errors_are_counted():
    solver = ScriptedSolver([SolveFailed("timeout", "slow"), SolveFailed("timeout", "slow"), "tok"])
    run_solve(solver, timeout=80)
    assert solver.attempt_errors == {"timeout: slow": 2}


def test_page_errors_are_retried_once():
    solver = ScriptedSolver([SolveFailed("page_error", "navigation"), "tok"])
    assert run_solve(solver).attempts == 2
    solver = ScriptedSolver([SolveFailed("page_error", "csp"), SolveFailed("page_error", "csp"), "tok"])
    with pytest.raises(BackendError) as e:
        run_solve(solver)
    assert e.value.code == "page_error" and len(solver.budgets) == 2


def test_warm_up_failure_is_retried(monkeypatch):
    import sys as _sys
    import types

    calls = {"n": 0}

    def get_user_agent():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Service /app/chromedriver unexpectedly exited")
        return "UA-ok"

    class Options:
        def __init__(self):
            self.arguments = []

        def add_argument(self, arg):
            self.arguments.append(arg)

    fake_utils = types.SimpleNamespace(get_user_agent=get_user_agent, uc=types.SimpleNamespace(ChromeOptions=Options))
    monkeypatch.setitem(_sys.modules, "utils", fake_utils)
    solver = TurnstileSolver(Slots(1, 0), 1, "/nonexistent")

    solver.warm_up_retry_interval = 0.01

    async def go():
        await solver.start()
        assert not solver.available and "chromedriver" in solver.unavailable_reason
        # 没有任何请求,后台也会重试初始化
        for _ in range(100):
            if solver.available:
                break
            await asyncio.sleep(0.01)
        solver._solve_blocking = lambda url, config, budget: ("tok", "page")
        result = await solver.solve("https://a.com/", "k", None, None, 30, 20)
        await solver.stop()
        return result

    result = asyncio.run(go())
    assert solver.available and solver.user_agent == "UA-ok" and result.token == "tok"
    # 浏览器以空白页启动(见 START_PAGE_ARGS)
    assert fake_utils.uc.ChromeOptions().arguments == ["about:blank"]


def test_interactive_open_follows_widget_events():
    from app.turnstile import _interactive_open

    assert not _interactive_open([])
    assert not _interactive_open(["init", "requestExtraParams", "food"])  # 卡在 food:复选框未出现
    assert _interactive_open(["init", "food", "interactiveBegin"])
    assert not _interactive_open(["init", "food", "interactiveBegin", "food", "interactiveEnd", "complete"])
    assert _interactive_open(["interactiveBegin", "interactiveEnd", "init", "food", "interactiveBegin"])  # 重置后再次出现


def test_install_chrome_args(tmp_path):
    import types

    from app.turnstile import install_chrome_args

    class BaseOptions:
        def __init__(self):
            self.arguments = []

        def add_argument(self, arg):
            self.arguments.append(arg)

    utils = types.SimpleNamespace(uc=types.SimpleNamespace(ChromeOptions=BaseOptions))
    args_file = tmp_path / "args"
    install_chrome_args(utils, ["--a"], str(args_file))
    install_chrome_args(utils, ["--a"], str(args_file))  # 重复调用不会叠加
    assert utils.uc.ChromeOptions().arguments == ["--a"]
    args_file.write_text("--b\n\n--host-resolver-rules=MAP *.example ~NOTFOUND\n")
    assert utils.uc.ChromeOptions().arguments == ["--a", "--b", "--host-resolver-rules=MAP *.example ~NOTFOUND"]


def test_navigation_timeout_retries_same_page():
    """刚启动的浏览器第一次导航超时后,先重试同一个页面,而不是直接换后备页面。"""
    import types

    class TimeoutException(Exception):
        pass

    class Driver:
        def __init__(self):
            self.visits = []

        def set_page_load_timeout(self, seconds):
            pass

        def get(self, url):
            self.visits.append(url)
            if len(self.visits) == 1:
                raise TimeoutException("timeout: page load")

        def execute_script(self, script, *args):
            return "a.com" if "location.hostname" in script else None

        def quit(self):
            pass

    driver = Driver()
    solver = TurnstileSolver(Slots(1, 0), 1, "/nonexistent")
    solver._utils = types.SimpleNamespace(get_webdriver=lambda: driver)
    solver._wait_for_token = lambda driver, deadline, load_deadline, attempt_id: ("token:tok", {"clicks": 1})
    token, page = solver._solve_blocking("https://a.com/login", {"sitekey": "k"}, 30)
    assert token == "tok" and page == "https://a.com/robots.txt"
    assert driver.visits == ["https://a.com/robots.txt", "https://a.com/robots.txt"]


class ChallengeDriver:
    """模拟停在 Cloudflare 验证页上的浏览器:前 challenge_checks 次检查仍是验证页。"""

    def __init__(self, challenge_checks):
        self.visits = []
        self.left = challenge_checks
        self.injected = False

    def set_page_load_timeout(self, seconds):
        pass

    def get(self, url):
        self.visits.append(url)

    def execute_script(self, script, *args):
        if "_cf_chl_opt" in script:
            self.left -= 1
            return self.left >= 0
        if "location.hostname" in script:
            return "a.com"
        self.injected = True
        return None

    def quit(self):
        pass


def fast_clock(monkeypatch):
    import app.turnstile as ts

    clock = {"t": 1000.0}
    monkeypatch.setattr(ts.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ts.time, "sleep", lambda s: clock.update(t=clock["t"] + s))


def test_challenge_page_fails_fast(monkeypatch):
    import types

    fast_clock(monkeypatch)
    driver = ChallengeDriver(challenge_checks=10**6)
    solver = TurnstileSolver(Slots(1, 0), 1, "/nonexistent")
    solver._utils = types.SimpleNamespace(get_webdriver=lambda: driver)
    with pytest.raises(SolveFailed) as e:
        solver._solve_blocking("https://a.com/login", {"sitekey": "k"}, 35)
    assert e.value.code == "challenge_page" and "5 秒盾" in e.value.message
    assert driver.visits == ["https://a.com/robots.txt", "https://a.com/login"] and not driver.injected


def test_challenge_page_that_clears_is_solved(monkeypatch):
    import types

    fast_clock(monkeypatch)
    driver = ChallengeDriver(challenge_checks=6)  # 约 3 秒后自动通过
    solver = TurnstileSolver(Slots(1, 0), 1, "/nonexistent")
    solver._utils = types.SimpleNamespace(get_webdriver=lambda: driver)
    solver._wait_for_token = lambda driver, deadline, load_deadline, attempt_id: ("token:tok", {"clicks": 1})
    token, page = solver._solve_blocking("https://a.com/login", {"sitekey": "k"}, 35)
    assert token == "tok" and page == "https://a.com/robots.txt" and driver.injected


def test_challenge_page_is_not_retried():
    solver = ScriptedSolver([SolveFailed("challenge_page", "behind challenge"), "tok"])
    with pytest.raises(BackendError) as e:
        run_solve(solver)
    assert e.value.code == "challenge_page" and len(solver.budgets) == 1


def test_solve_log_keeps_recent_and_minute_buckets():
    log = SolveLog(recent=2, keep_minutes=10)
    log.record("https://a.com/x", "k1", True, 10.0, 1, now=600.0)
    log.record("https://a.com/x", "k1", True, 20.0, 2, now=630.0)
    log.record("https://b.com/", "k2", False, 30.0, 3, "timeout: t", now=700.0)
    snap = log.snapshot()
    assert [r["host"] for r in snap["recent"]] == ["b.com", "a.com"]  # 只保留最近 2 条,新的在前
    assert snap["recent"][0]["ok"] is False and snap["recent"][0]["error"] == "timeout: t"
    assert snap["minutes"] == [[600, 2, 0, 30.0], [660, 0, 1, 0.0]]
    log.record("https://a.com/", "k1", True, 5.0, 1, now=600.0 + 11 * 60)  # 超过 10 分钟的分钟计数被清理
    assert [m[0] for m in log.snapshot()["minutes"]] == [660, 1260]


def test_admin_stats(make_client, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"phase": "ready", "slot": "b", "tunnel_token": "TUNNEL_TOKEN_B", "fleet_version": "9"}))
    with make_client(agent_state_file=str(state)) as c:
        assert c.get("/admin/stats").status_code == 401
        assert c.post("/solve", json=SOLVE, headers=AUTH).status_code == 200
        r = c.get("/admin/stats", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["health"]["status"] == "ok" and data["health"]["solved"] == 1
        assert data["agent"] == {"phase": "ready", "slot": "b", "fleet_version": "9"}  # 不返回隧道令牌来源
        assert data["recent"][0]["host"] == "example.com" and data["recent"][0]["ok"] is True
        assert data["minutes"][0][1] == 1
    with make_client(agent_state_file=str(tmp_path / "missing.json")) as c:
        assert c.get("/admin/stats", headers=AUTH).json()["agent"] is None


def test_solver_records_history():
    solver = ScriptedSolver(["tok"])
    run_solve(solver)
    failing = ScriptedSolver([SolveFailed("turnstile_error", "110200")])
    with pytest.raises(BackendError):
        run_solve(failing)
    ok, = solver.history.snapshot()["recent"]
    bad, = failing.history.snapshot()["recent"]
    assert ok["ok"] is True and ok["attempts"] == 1 and ok["sitekey"] == "0xKEY"
    assert bad["ok"] is False and bad["error"].startswith("turnstile_error")


def test_draining_worker_refuses_new_work_but_serves_results(make_client, tmp_path):
    drain = tmp_path / "drain"
    with make_client(drain_file=str(drain)) as c:
        task = {"type": "TurnstileTaskProxyless", "websiteURL": "https://a.com/", "websiteKey": sk("wait")}
        created = c.post("/createTask", json={"clientKey": KEY, "task": task}).json()
        assert created["errorId"] == 0
        drain.touch()
        assert c.get("/health").json()["draining"] is True
        refused = c.post("/createTask", json={"clientKey": KEY, "task": task}).json()
        assert refused["errorCode"] == "ERROR_NO_SLOT_AVAILABLE"
        assert c.post("/solve", json=SOLVE, headers=AUTH).status_code == 429
        # 已创建的任务照常返回结果
        for _ in range(50):
            result = c.post("/getTaskResult", json={"clientKey": KEY, "taskId": created["taskId"]}).json()
            if result.get("status") == "ready":
                break
            time.sleep(0.05)
        assert result["status"] == "ready"
