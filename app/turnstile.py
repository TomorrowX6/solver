"""按 sitekey 直接求解 Turnstile。

使用 FlareSolverr 的反检测浏览器(undetected-chromedriver,与 FlareSolverr 完全相同的启动参数):
打开目标域名下的一个页面(默认先试 /robots.txt,失败再用调用方给的 url),清空页面后用指定 sitekey 渲染组件,
用键盘(Tab + 空格)或鼠标点击复选框,拿到 token 后返回。页面属于目标域名,Turnstile 的域名校验可以通过。

Selenium 是同步阻塞的,每次求解在线程池里运行,并占用一个与 /v1 共用的浏览器名额。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .backend import BackendError, Slots
from .stats import SolveLog

log = logging.getLogger("gateway.turnstile")

# Turnstile 客户端错误码中属于配置错误的前缀(sitekey 无效、域名未授权、action/cData 非法等),重试没有意义
FATAL_ERROR_PREFIXES = ("110", "400")

# 两种注入方式共用的页面脚本:状态对象、组件渲染回调、api.js 加载器
_STATE_JS = (
    "window.__ts = { token: null, error: null, errors: 0, interactive: false, afterInteractive: false,"
    " loaded: false, widget: null,"
    " loadRetries: 0, loadStalls: 0, loadAttempt: 0, events: [], startedAt: Date.now(), lastEventAt: 0 };"
    # 组件 iframe 通过 postMessage 向页面报告状态,记下事件名(相邻重复的合并),用于判断复选框何时可点
    "window.addEventListener('message', function (e) {"
    "  if (e.origin !== 'https://challenges.cloudflare.com' || !e.data || typeof e.data !== 'object') return;"
    "  const name = String(e.data.event || e.data.type || '?');"
    "  const ev = window.__ts.events;"
    "  if (ev[ev.length - 1] !== name && ev.length < 60) { ev.push(name); window.__ts.lastEventAt = Date.now(); }"
    "});"
)

_ONLOAD_JS = r"""
window.__tsOnload = function () {
  if (window.__ts.loaded) return;  // 重复加载的 api.js 只渲染一次
  window.__ts.loaded = true;
  const opts = Object.assign({}, cfg);
  opts.callback = function (t) { window.__ts.token = t; };
  opts["error-callback"] = function (code) { window.__ts.error = String(code); window.__ts.errors++; return true; };
  opts["expired-callback"] = function () { window.__ts.token = null; };
  // 进入交互模式(出现复选框)/ 离开交互模式(点击已被接受,正在验证)
  opts["before-interactive-callback"] = function () { window.__ts.interactive = true; window.__ts.afterInteractive = false; };
  opts["after-interactive-callback"] = function () { window.__ts.afterInteractive = true; };
  try { window.__ts.widget = turnstile.render("#cf-ts", opts); }
  catch (e) { window.__ts.error = "render:" + ((e && e.message) || e); }
};
"""

# api.js:加载失败按 1、2、3、4 秒退避重试;6 秒既没成功也没报错(连接卡住)时换一个等价地址(仅参数顺序不同)再请求
_LOADER_JS = r"""
window.__tsSrc = [
  "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__tsOnload&render=explicit",
  "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__tsOnload"
];
window.__tsLoad = function (n) {
  window.__ts.loadAttempt = n;
  const s = document.createElement("script");
  s.src = window.__tsSrc[n % window.__tsSrc.length];
  s.async = true;
  s.onerror = function () {
    if (window.__ts.loaded || window.__ts.loadAttempt !== n) return;
    window.__ts.loadRetries++;
    if (n < 4) { setTimeout(function () { window.__tsLoad(n + 1); }, 1000 * (n + 1)); }
    else { window.__ts.error = "script_load_failed"; }
  };
  document.head.appendChild(s);
  setTimeout(function () {
    if (!window.__ts.loaded && window.__ts.loadAttempt === n && n < 4) {
      window.__ts.loadStalls++;
      window.__tsLoad(n + 1);
    }
  }, 6000);
};
window.__tsLoad(0);
"""

# 注入方式 innerhtml:替换当前文档的内容
_INJECT_JS = (
    "const cfg = arguments[0];\n"
    + _STATE_JS
    + """
document.documentElement.innerHTML =
  '<head><meta charset="utf-8"><title>Turnstile</title></head>' +
  '<body style="margin:0;padding:24px"><div id="cf-ts"></div></body>';
"""
    + _ONLOAD_JS
    + _LOADER_JS
)

# 注入方式 write:document.open/write 重新解析出一份完整的 HTML 文档(对纯文本页面也是真正的 HTML 文档)
_PAGE_TEMPLATE = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Turnstile</title>\n<script>\nconst cfg = __CONFIG__;\n'
    + _STATE_JS
    + _ONLOAD_JS
    + '</script>\n</head><body style="margin:0;padding:24px"><div id="cf-ts"></div>\n<script>'
    + _LOADER_JS
    + "</script>\n</body></html>"
)

_WRITE_JS = "document.open(); document.write(arguments[0]); document.close();"

# 读取页面状态。token 除了回调,还从 turnstile.getResponse() 与组件内的隐藏输入框读取(回调偶尔没有触发),
# tokenSource 记录来源,便于判断是否漏收
_POLL_JS = r"""
const s = window.__ts;
if (!s) return null;
s.eventAge = Date.now() - (s.lastEventAt || s.startedAt);
if (s.token) { s.tokenSource = s.tokenSource || "callback"; return s; }
try {
  const r = window.turnstile && s.widget !== null && turnstile.getResponse(s.widget);
  if (r) { s.token = r; s.tokenSource = "getResponse"; return s; }
} catch (e) {}
const input = document.querySelector('#cf-ts [name="cf-turnstile-response"]');
if (input && input.value) { s.token = input.value; s.tokenSource = "input"; }
return s;
"""


def widget_page(config: dict) -> str:
    # config 中的值已在模型层限制了字符集,这里再转义 "</" 作为兜底
    return _PAGE_TEMPLATE.replace("__CONFIG__", json.dumps(config).replace("</", "<\\/"))


@dataclass
class SolveResult:
    token: str
    elapsed: float
    user_agent: str | None
    attempts: int
    page: str


def _interactive_open(events: list[str]) -> bool:
    """最近一次 interactiveBegin 之后还没有 interactiveEnd:复选框正显示,等待点击。"""
    begin = max((i for i, e in enumerate(events) if e == "interactiveBegin"), default=-1)
    end = max((i for i, e in enumerate(events) if e == "interactiveEnd"), default=-1)
    return begin > end


class SolveFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def candidate_pages(url: str, mode: str) -> list[str]:
    """承载组件的页面:light = 同域名的 /robots.txt(轻、通常没有 CSP),full = 调用方给的 url。"""
    parts = urlsplit(url)
    light = f"{parts.scheme}://{parts.netloc}/robots.txt"
    if mode == "light":
        return [light]
    if mode == "full":
        return [url]
    return [light, url] if light != url else [url]


# 浏览器启动时打开空白页,而不是新标签页:新标签页会请求 www.google.com 等(国内网络下连接挂起),
# chromedriver 要等这个初始导航结束才执行 driver.get,实测约 1/5 的浏览器第一次打开页面就超时 17 秒。
# 同一节点对比:默认 7 次导航超时 / 9 次尝试失败,空白页 0 / 0(各 20 个任务)
START_PAGE_ARGS = ["about:blank"]


def install_chrome_args(utils: Any, static_args: list[str], args_file: str = "") -> None:
    """给 FlareSolverr 的浏览器追加启动参数。

    utils.get_webdriver() 内部用 uc.ChromeOptions() 构造参数且不接受额外参数,这里把本进程内的
    ChromeOptions 换成追加参数的子类。args_file 每行一个参数(参数本身可含空格),
    存在时每次启动都重新读取(便于在同一环境里对比实验)。
    """
    base = utils.uc.ChromeOptions
    if getattr(base, "_ts_extra_args", False):
        return

    class ChromeOptions(base):  # type: ignore[misc, valid-type]
        _ts_extra_args = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            extra = list(static_args)
            if args_file and os.path.exists(args_file):
                with open(args_file, encoding="utf-8") as f:
                    extra += [line.strip() for line in f if line.strip()]
            for arg in extra:
                self.add_argument(arg)

    utils.uc.ChromeOptions = ChromeOptions


class TurnstileSolver:
    def __init__(
        self,
        slots: Slots,
        max_workers: int,
        flaresolverr_dir: str,
        page_mode: str = "auto",
        inject: str = "write",
        click_mode: str = "mouse",
        debug_dir: str = "",
        chrome_args: str = "",
        chrome_args_file: str = "",
        stall_seconds: float = 20.0,
    ) -> None:
        self.slots = slots
        self.page_mode = page_mode
        self.inject = inject
        # mouse(默认):鼠标点击;keyboard:Tab + 空格;alternate:两者交替。实测键盘方式在注入页面上基本无效
        self.click_mode = click_mode
        # 设置后保存组件截图(点击前、每次点击后、放弃时),用于排查「点击后无 token」
        self.debug_dir = debug_dir
        # 追加给浏览器的启动参数(空格分隔)
        self.chrome_args = chrome_args.split()
        self.chrome_args_file = chrome_args_file
        # 复选框出现前多久没有新事件判定为卡住,换新浏览器重试
        self.stall_seconds = stall_seconds
        # 拿到 token 时已点击的次数与 token 来源的计数,在 /health 中展示
        self.token_stats: dict[str, int] = {}
        self._flaresolverr_dir = flaresolverr_dir
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="turnstile")
        self._utils: Any = None
        self.unavailable_reason: str | None = None
        self._retry_warm_up = True
        self._warm_up_task: asyncio.Task | None = None
        self.warm_up_retry_interval = 15.0
        self.user_agent: str | None = None
        self.solved = 0
        self.failed = 0
        # 每次尝试失败的原因计数(含随后重试成功的),在 /health 中展示,便于线上定位
        self.attempt_errors: dict[str, int] = {}
        # 最近的求解明细与每分钟计数,供后台面板展示
        self.history = SolveLog()

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        await self._warm_up()
        if not self.available and self._retry_warm_up:
            self._warm_up_task = asyncio.create_task(self._retry_warm_up_loop())

    async def _retry_warm_up_loop(self) -> None:
        """初始化失败(例如 chromedriver 首次改写时的偶发冲突)后在后台定时重试。

        不能等请求来触发:求解器不可用时 /health 为 degraded,守护进程不会发自检请求,任务也会被直接拒绝。
        """
        while not self.available and self._retry_warm_up:
            await asyncio.sleep(self.warm_up_retry_interval)
            await self._warm_up()

    async def _warm_up(self) -> None:
        """导入 FlareSolverr 的浏览器工具并预热一次(确定 UA、chromedriver 路径),避免并发首次调用时竞争。"""
        try:
            if self._flaresolverr_dir not in sys.path:
                sys.path.insert(0, self._flaresolverr_dir)
            import utils  # type: ignore[import-not-found]  # FlareSolverr 的 src/utils.py

            install_chrome_args(utils, START_PAGE_ARGS + self.chrome_args, self.chrome_args_file)
            self.user_agent = await asyncio.get_running_loop().run_in_executor(self._executor, utils.get_user_agent)
            self._utils = utils
            self.unavailable_reason = None
            log.info("turnstile solver ready, UA=%s", self.user_agent)
        except ImportError as e:
            # 本地开发环境没有 FlareSolverr:/solve 不可用,不再重试
            self._utils = None
            self.unavailable_reason = f"{type(e).__name__}: {e}"
            self._retry_warm_up = False
            log.warning("turnstile solver unavailable: %s", self.unavailable_reason)
        except Exception as e:  # noqa: BLE001
            self._utils = None
            self.unavailable_reason = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
            log.warning("turnstile solver warm-up failed, will retry: %s", self.unavailable_reason)

    async def stop(self) -> None:
        if self._warm_up_task:
            self._warm_up_task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def available(self) -> bool:
        return self._utils is not None

    # ------------------------------------------------------------------ 求解
    async def solve(
        self,
        url: str,
        sitekey: str,
        action: str | None,
        cdata: str | None,
        timeout: float,
        attempt_timeout: float,
        admit_check: bool = True,
    ) -> SolveResult:
        if not self.available:
            raise BackendError("solver_unavailable", f"求解器不可用: {self.unavailable_reason}")
        config: dict[str, str] = {"sitekey": sitekey, "retry": "auto", "refresh-expired": "auto"}
        if action:
            config["action"] = action
        if cdata:
            config["cData"] = cdata

        async with self.slots.hold(check=admit_check):
            started = time.monotonic()
            deadline = started + timeout
            attempts = 0
            last: SolveFailed | None = None
            # 一个浏览器卡住时换新浏览器重试,比在一个浏览器里死等更有效;剩余时间不足 15 秒就不再重试
            while deadline - time.monotonic() >= 15 or attempts == 0:
                attempts += 1
                budget = min(attempt_timeout, deadline - time.monotonic())
                try:
                    token, page = await asyncio.get_running_loop().run_in_executor(
                        self._executor, self._solve_blocking, url, config, budget
                    )
                except Exception as e:  # noqa: BLE001
                    if not isinstance(e, SolveFailed):
                        # 浏览器崩溃、WebDriver 异常等:换新浏览器重试
                        e = SolveFailed("browser_error", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")
                    last = e
                    reason = re.sub(r"(组件卡住)\(.*?\)", r"\1", f"{e.code}: {e.message}")[:80]
                    self.attempt_errors[reason] = self.attempt_errors.get(reason, 0) + 1
                    log.warning("attempt %d failed for %s [%s]: %s", attempts, url, e.code, e.message)
                    # 配置错误是确定性的,不重试;页面加载不了组件可能是网络暂时不通(重试一次),
                    # 也可能是 CSP 拦截(重试也没用),所以最多重试一次
                    if e.code == "turnstile_error" or (e.code == "page_error" and attempts >= 2):
                        break
                    continue
                self.solved += 1
                elapsed = round(time.monotonic() - started, 3)
                log.info("solved %s in %.1fs (%d attempts, page=%s)", url, elapsed, attempts, page)
                self.history.record(url, sitekey, True, elapsed, attempts)
                return SolveResult(token, elapsed, self.user_agent, attempts, page)

        self.failed += 1
        assert last is not None
        self.history.record(url, sitekey, False, time.monotonic() - started, attempts, f"{last.code}: {last.message}")
        raise BackendError(last.code, f"{last.message}(共尝试 {attempts} 次)")

    def _solve_blocking(self, url: str, config: dict, budget: float) -> tuple[str, str]:
        deadline = time.monotonic() + budget
        attempt_id = f"{time.strftime('%H%M%S')}-{random.randrange(16**4):04x}"
        launch_started = time.monotonic()
        driver = self._utils.get_webdriver()
        launch = time.monotonic() - launch_started
        try:
            driver.set_page_load_timeout(max(5, min(20, budget / 2)))
            target_host = urlsplit(url).hostname
            last_error = "no_page"
            pages = candidate_pages(url, self.page_mode)
            for page in pages:
                if time.monotonic() >= deadline:
                    break
                page_started = time.monotonic()
                error = self._open(driver, page, attempt_id)
                if error and "Timeout" in error and time.monotonic() < deadline:
                    # 导航超时多半与页面无关:刚启动的浏览器偶尔卡在启动页上,导航请求根本没有发出;
                    # 超时之后再导航一次通常立即成功,先重试同一个页面再换后备页面
                    page_started = time.monotonic()
                    error = self._open(driver, page, attempt_id)
                if error:
                    last_error = f"navigation: {error}"
                    continue
                if driver.execute_script("return location.hostname") != target_host:
                    last_error = "redirected_to_other_host"
                    continue
                if self.inject == "write":
                    driver.execute_script(_WRITE_JS, widget_page(config))
                else:
                    driver.execute_script(_INJECT_JS, config)
                # 还有后备页面时,组件 12 秒内没加载出来就换页面,把时间留给后面的页面
                load_deadline = deadline if page == pages[-1] else min(deadline, time.monotonic() + 12)
                result, state = self._wait_for_token(driver, deadline, load_deadline, attempt_id)
                if result.startswith("token:"):
                    key = f"clicks={state.get('clicks')} via={state.get('tokenSource')}"
                    self.token_stats[key] = self.token_stats.get(key, 0) + 1
                    log.info(
                        "attempt %s token on %s after %.1fs (browser %.1fs): %s events=%s",
                        attempt_id, page, time.monotonic() - page_started, launch, key, ",".join(state.get("events") or []),
                    )
                elif result.startswith("timeout"):
                    log.info(
                        "attempt %s gave up on %s (browser %.1fs): %s clicks=%s errors=%s error=%s events=%s",
                        attempt_id, page, launch, result, state.get("clicks"), state.get("errors"), state.get("error"),
                        ",".join(state.get("events") or []),
                    )
                else:
                    log.info(
                        "attempt %s no widget on %s after %.1fs: %s loaded=%s error=%s load_attempt=%s events=%s",
                        attempt_id, page, time.monotonic() - page_started, result, state.get("loaded"),
                        state.get("error"), state.get("loadAttempt"), ",".join(state.get("events") or []),
                    )
                if state.get("loadRetries") or state.get("loadStalls"):
                    log.info(
                        "api.js load retries=%s stalls=%s on %s (%s)",
                        state.get("loadRetries"), state.get("loadStalls"), page, result.split(":")[0],
                    )
                if result.startswith("token:"):
                    return result[6:], page
                last_error = result
                if result.startswith("fatal:"):
                    raise SolveFailed("turnstile_error", f"Turnstile 返回错误 {result[6:]}(检查 sitekey 与域名是否匹配)")
                if result != "script_load_failed":
                    break  # 组件已加载但没拿到 token,换页面没有意义,交给上层换新浏览器重试
            if last_error.startswith("timeout"):
                reason = last_error.partition(":")[2] or "未知原因"
                raise SolveFailed("timeout", f"未能在限定时间内拿到 token({reason})")
            raise SolveFailed("page_error", f"无法在目标域名上加载 Turnstile 组件({last_error})")
        finally:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _open(driver, page: str, attempt_id: str) -> str | None:
        """打开页面,失败时返回异常类型名(页面加载超时或网络错误)。"""
        started = time.monotonic()
        try:
            driver.get(page)
            return None
        except Exception as e:  # noqa: BLE001
            log.info("attempt %s could not open %s after %.1fs: %s", attempt_id, page, time.monotonic() - started, type(e).__name__)
            return type(e).__name__

    def _wait_for_token(self, driver, deadline: float, load_deadline: float, attempt_id: str = "") -> tuple[str, dict]:
        """返回 (结果, 最后一次的页面状态)。

        结果为 token:<值> / fatal:<错误码> / script_load_failed / timeout:<原因>;
        load_deadline 之前 api.js 仍未加载完成时返回 script_load_failed(调用方换下一个页面)。

        点击由组件 iframe 的 postMessage 事件驱动:收到 interactiveBegin(复选框出现)后才点,
        复选框出现前点击无效。挑战超过 8 秒没有新事件且不在交互阶段时(实测多卡在 food 之后,
        同一浏览器里 turnstile.reset() 后会在同一处再次卡住),直接放弃,交给上层换新浏览器重试。
        """
        state: dict = {}
        clicks = 0
        phase_begins = 0  # 已处理过的 interactiveBegin 次数(每次出现复选框算一个交互阶段)
        phase_clicks = 0
        click_at: float | None = None
        loaded_at: float | None = None
        last_reset_errors = 0
        shot_at: float | None = None
        reason = ""
        while time.monotonic() < deadline:
            state = driver.execute_script(_POLL_JS) or {}
            state["clicks"] = clicks
            if state.get("token"):
                return "token:" + state["token"], state
            err = state.get("error") or ""
            if err == "script_load_failed" or err.startswith("render:"):
                return "script_load_failed", state
            if err and err.startswith(FATAL_ERROR_PREFIXES):
                return "fatal:" + err, state
            if not state.get("loaded"):
                if time.monotonic() > load_deadline:
                    return "script_load_failed", state
                time.sleep(0.5)
                continue

            now = time.monotonic()
            loaded_at = loaded_at or now
            events = state.get("events") or []
            if state.get("errors", 0) > last_reset_errors and state.get("widget") is not None:
                # 组件报错(如 600010)后重置,重新跑一次挑战
                last_reset_errors = state["errors"]
                self._reset_widget(driver)
                click_at = None
            elif _interactive_open(events):
                begins = events.count("interactiveBegin")
                if begins > phase_begins:
                    # 复选框刚出现:像真人一样停顿 1~2 秒再点
                    phase_begins, phase_clicks = begins, 0
                    click_at = now + random.uniform(1.0, 2.0)
                if click_at and now >= click_at:
                    if phase_clicks < 3:
                        if clicks == 0:
                            self._shot(driver, attempt_id, "0-pre")
                        self._click(driver, keyboard=self._use_keyboard(clicks))
                        clicks += 1
                        phase_clicks += 1
                        shot_at = now + 4.0
                        # 点了 6 秒还没进入验证(没有 interactiveEnd),再点一次
                        click_at = now + random.uniform(5.5, 7.0)
                    else:
                        reason = "点击后无响应"
                        break
            elif not events and clicks == 0 and now - loaded_at > 8:
                # 没收到任何组件事件(消息格式变化等):退回按时间点击
                self._click(driver, keyboard=self._use_keyboard(clicks))
                clicks += 1
            elif state.get("eventAge", 0) > self.stall_seconds * 1000 and state.get("widget") is not None:
                reason = f"组件卡住({','.join(events[-3:])})"
                break
            if shot_at and now >= shot_at:
                self._shot(driver, attempt_id, f"{clicks}-after")
                shot_at = None
            time.sleep(0.5)

        self._shot(driver, attempt_id, "9-end")
        # 细分超时原因,写进错误信息,/health 的 attempt_errors 按原因计数
        if not reason:
            if not state.get("loaded"):
                reason = "api.js 未加载"
            elif state.get("errors"):
                reason = f"组件报错 {state.get('error')}"
            elif clicks:
                reason = "点击后无 token"
            else:
                reason = "组件未就绪"
        return f"timeout:{reason}", state

    @staticmethod
    def _reset_widget(driver) -> None:
        driver.execute_script(
            "try { window.__ts.lastEventAt = Date.now(); turnstile.reset(window.__ts.widget) } catch (e) {}"
        )

    def _use_keyboard(self, clicks: int) -> bool:
        if self.click_mode == "keyboard":
            return True
        if self.click_mode == "mouse":
            return False
        return clicks % 2 == 0

    def _click(self, driver, keyboard: bool) -> None:
        from selenium.webdriver.common.action_chains import ActionChains  # type: ignore[import-not-found]
        from selenium.webdriver.common.by import By  # type: ignore[import-not-found]
        from selenium.webdriver.common.keys import Keys  # type: ignore[import-not-found]

        try:
            if keyboard:
                # 与 FlareSolverr 相同:焦点回到页面后 Tab 到组件,再按空格
                driver.execute_script("document.activeElement && document.activeElement.blur(); window.focus();")
                ActionChains(driver).pause(random.uniform(0.2, 0.6)).send_keys(Keys.TAB).pause(
                    random.uniform(0.2, 0.5)
                ).send_keys(Keys.SPACE).perform()
            else:
                # 鼠标从组件右下方分几步移到复选框(组件左侧约 28px、垂直居中),停顿后按下再松开
                box = driver.find_element(By.ID, "cf-ts")
                width = box.size.get("width") or 300
                target_x = -width / 2 + 28 + random.uniform(-3, 3)
                target_y = random.uniform(-3, 3)
                chain = ActionChains(driver).move_to_element_with_offset(
                    box, target_x + random.uniform(120, 200), target_y + random.uniform(30, 60)
                )
                for frac in (0.55, 0.8, 0.95, 1.0):
                    chain = chain.pause(random.uniform(0.05, 0.15)).move_to_element_with_offset(
                        box,
                        target_x + (1 - frac) * random.uniform(120, 200),
                        target_y + (1 - frac) * random.uniform(30, 60),
                    )
                chain.pause(random.uniform(0.15, 0.4)).click_and_hold().pause(random.uniform(0.06, 0.14)).release().perform()
        except Exception as e:  # noqa: BLE001 - 点击失败下一轮再试
            log.debug("click failed: %s", e)

    def _shot(self, driver, attempt_id: str, tag: str) -> None:
        if not self.debug_dir:
            return
        try:
            from selenium.webdriver.common.by import By  # type: ignore[import-not-found]

            os.makedirs(self.debug_dir, exist_ok=True)
            png = driver.find_element(By.ID, "cf-ts").screenshot_as_png
            with open(os.path.join(self.debug_dir, f"{attempt_id}-{tag}.png"), "wb") as f:
                f.write(png)
        except Exception as e:  # noqa: BLE001 - 截图失败不影响求解
            log.debug("screenshot failed: %s", e)


def solve_response(result: SolveResult) -> dict:
    return {
        "token": result.token,
        "elapsed": result.elapsed,
        "attempts": result.attempts,
        "user_agent": result.user_agent,
    }
