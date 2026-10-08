// Cloudflare Worker:在多条隧道之间分流(后端为 Turnstile 网关),并提供用户、令牌与积分(参照 NewAPI)。
//
// 路由:solver.000.moe/*(用 deploy/wrangler.toml 部署)
//
// 对路由自身域名的子请求会直接发往源站(隧道 A),不会再次触发本 Worker;其余隧道通过各自的主机名访问。
//   * POST /createTask:随机分流,无空闲名额时改投;POST /getTaskResult:按 taskId 第一位(位置编号)路由回原 worker;
//   * POST /v1:5 秒盾(FlareSolverr 接口)已下线,直接返回 404;
//   * GET /(浏览器打开,Accept 含 text/html):控制台页面,旧入口 /admin 跳转到这里;/api/*:控制台接口(见 api.js);
//   * 其他请求:随机打乱各条隧道的顺序依次尝试,遇到可重试的结果就换下一条。
//
// 计费:初始化(控制台首次使用)后,用户令牌 sk-… 调用接口时预扣积分,成功结算、失败退还;
// worker 的 API Key(根密钥)照常可用且不计费。未初始化时行为与之前相同。

import ADMIN_HTML from "./admin.html";
import { handleApi } from "./api.js";
import { LOG, addLog, authenticate, getChannels, getOptions, intOption, now, refund, reserve, rootKey, safeEqual, settle, sweep } from "./db.js";

// 位置 → 主机名;位置 a 为 null,表示使用路由自身的域名(隧道 A)。本地调试可用变量 SLOT_HOSTS(JSON)覆盖。
// a–d 为 CNB worker;自有服务器渠道(e–p)来自 D1 的 channels 表
const SLOT_HOSTS = { a: null, b: "solver-b.000.moe", c: "solver-c.000.moe", d: "solver-d.000.moe" };
// taskId 第一位是位置序号(十六进制),与 fleet/agent.py 的 SLOT_ORDER 一致
const SLOT_ORDER = "abcdefghijklmnop";

// 改投下一条隧道的情况:429 = 该 worker 槽位与排队都已满;503 = 该 worker 的求解器不可用;
// 其余为 Cloudflare 生成的源站故障(530 = 隧道没有可用连接器)。网关的 500 等是确定结果,原样返回
const RETRY_STATUS = new Set([429, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530]);

// 打码平台风格接口(HTTP 一律 200,用 errorCode 表示错误):这些错误码说明该 worker 没处理请求,可以改投
const TASK_RETRY_CODES = new Set(["ERROR_NO_SLOT_AVAILABLE", "ERROR_SERVICE_UNAVALIABLE"]);

// Turnstile 不支持经调用方的代理求解。网关同样拒绝;用户令牌的请求在这里先拦下,
// 还没重装升级的服务器渠道也就不会再接受代理
const PROXY_TASK_TYPES = new Set(["turnstiletask", "antiturnstiletask"]);

// 明显填错的参数直接拒绝,不占用 worker(网关同样检查 sitekey 格式,这里让未升级的服务器渠道也一样):
// sitekey 正式的以 0x 开头,Cloudflare 的测试 sitekey 以 1x / 2x / 3x 开头;网址不能是本服务自己
const SITEKEY_RE = /^[0-3]x[0-9A-Za-z_-]{8,80}$/;
function badTarget(rt, websiteURL, sitekey) {
  if (typeof sitekey === "string" && sitekey && !SITEKEY_RE.test(sitekey.trim())) {
    return "websiteKey 格式不对：应为 0x 开头的 Turnstile sitekey(页面中 data-sitekey 的值)";
  }
  if (hostname(websiteURL) === rt.url.hostname) return "websiteURL 填成了本服务的地址：应为组件所在的目标页面";
  return null;
}

// 隧道没有连接器(位置未启用或 worker 已下线):30 秒内排到最后再试,避免每个请求都先撞一次
const OFFLINE_STATUS = new Set([502, 521, 522, 523, 530]);
const offlineUntil = new Map();
let lastScaleTrigger = 0;

// 控制台页面不含任何密钥;禁止被嵌入,只允许请求本域名
const ADMIN_PAGE_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "no-store",
  "content-security-policy":
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; " +
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "x-frame-options": "DENY",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  vary: "accept",
};

function shuffle(items) {
  for (let i = items.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [items[i], items[j]] = [items[j], items[i]];
  }
  return items;
}

const error = (status, code, message) => Response.json({ status: "error", message, code }, { status });
const taskError = (code, description) => Response.json({ errorId: 1, errorCode: code, errorDescription: description });

function parseJson(text) {
  try {
    const value = JSON.parse(text || "");
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function hostname(value) {
  try {
    return new URL(String(value)).hostname;
  } catch {
    return null;
  }
}

/** 一次请求的上下文:各位置的主机名与转发方法。 */
class Relay {
  constructor(request, env, ctx, url) {
    this.request = request;
    this.env = env;
    this.ctx = ctx;
    this.url = url;
    this.hosts = { ...(env.SLOT_HOSTS ? JSON.parse(env.SLOT_HOSTS) : SLOT_HOSTS) };
    this.cnbSlots = new Set(Object.keys(this.hosts));
    this.names = {};
    this.disabled = new Set();
    this.slots = Object.keys(this.hosts); // 接收新请求的位置
  }

  // 加入服务器渠道;禁用的渠道不再分配新请求,但已创建任务的结果仍可查询
  async init() {
    if (!this.env.DB) return;
    for (const ch of await getChannels(this.env)) {
      this.hosts[ch.slot] = ch.host;
      this.names[ch.slot] = ch.name;
      if (ch.status !== 1) this.disabled.add(ch.slot);
    }
    this.slots = Object.keys(this.hosts).filter((s) => !this.disabled.has(s));
  }

  hostOf(slot) {
    return this.hosts[slot] ?? this.url.host;
  }

  // 在线的位置随机排在前面,最近离线的排在最后
  order() {
    const t = Date.now();
    const online = this.slots.filter((s) => !(offlineUntil.get(s) > t));
    const offline = this.slots.filter((s) => offlineUntil.get(s) > t);
    return [...shuffle(online), ...shuffle(offline)];
  }

  mark(slot, resp) {
    if (!resp || OFFLINE_STATUS.has(resp.status)) offlineUntil.set(slot, Date.now() + 30000);
    else offlineUntil.delete(slot);
  }

  forward(host, body, apiKey) {
    const target = new URL(this.url);
    target.host = host;
    const headers = new Headers(this.request.headers);
    if (body !== undefined) headers.delete("content-length");
    if (apiKey) {
      headers.set("x-api-key", apiKey);
      headers.delete("authorization");
    }
    return fetch(target, { method: this.request.method, headers, body, redirect: "manual" });
  }

  // 随机打乱各条隧道依次尝试,遇到可重试的结果就换下一条
  async any(body, apiKey) {
    let last;
    for (const slot of this.order()) {
      try {
        const resp = await this.forward(this.hostOf(slot), body, apiKey);
        this.mark(slot, resp);
        if (!RETRY_STATUS.has(resp.status)) return resp;
        last = resp;
      } catch (err) {
        this.mark(slot, null);
        last = error(500, "upstream_error", String(err));
      }
    }
    this.saturated = true;
    return last;
  }

  async createTask(body) {
    let last;
    for (const slot of this.order()) {
      try {
        const resp = await this.forward(this.hostOf(slot), body);
        this.mark(slot, resp);
        if (RETRY_STATUS.has(resp.status)) continue;
        const text = await resp.text();
        last = new Response(text, { status: resp.status, headers: resp.headers });
        const data = parseJson(text);
        if (!data || !TASK_RETRY_CODES.has(data.errorCode)) return last;
      } catch (err) {
        this.mark(slot, null); // 连接失败,换下一个位置
      }
    }
    this.saturated = true; // 所有位置都满了或不可用:请轮换器扩容
    return last ?? taskError("ERROR_SERVICE_UNAVALIABLE", "所有 worker 均不可用，请稍后重试");
  }

  // taskId 第一位是位置编号(a=0、b=1……),查询必须回到创建任务的那台 worker
  async getTaskResult(body, taskId) {
    const slot = SLOT_ORDER[parseInt(String(taskId || "").charAt(0), 16)];
    if (!slot || !(slot in this.hosts)) return taskError("ERROR_TASKID_INVALID", "taskId 无效");
    try {
      const resp = await this.forward(this.hostOf(slot), body);
      if (!RETRY_STATUS.has(resp.status)) return resp;
    } catch (err) {
      // 连接失败,按 worker 已下线处理
    }
    return taskError("ERROR_TASKID_INVALID", "任务所在的 worker 已下线，请重新创建任务");
  }

  // 用根密钥校验:任一 worker 认可即有效(初始化时使用)
  async validateSolverKey(key) {
    for (const slot of this.slots) {
      const target = new URL("/getBalance", this.url);
      target.host = this.hostOf(slot);
      try {
        const resp = await fetch(target, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ clientKey: key }),
          signal: AbortSignal.timeout(8000),
        });
        const data = parseJson(await resp.text());
        if (data && data.errorId === 0) return true;
        if (data && data.errorCode === "ERROR_KEY_DOES_NOT_EXIST") return false;
      } catch {
        // 换下一个位置
      }
    }
    return false;
  }

  // 各位置的 /admin/stats(旧版本 worker 退回 /health)
  async fleet(key) {
    const one = async (slot) => {
      const target = new URL("/admin/stats", this.url);
      target.host = this.hostOf(slot);
      const started = Date.now();
      try {
        let resp = await fetch(target, { headers: key ? { "x-api-key": key } : {}, signal: AbortSignal.timeout(8000) });
        const latency = Date.now() - started;
        if (resp.status === 404) {
          target.pathname = "/health";
          resp = await fetch(target, { signal: AbortSignal.timeout(8000) });
          if (!resp.ok) return { error: `HTTP ${resp.status}`, latency };
          return { legacy: true, latency, health: await resp.json(), agent: null, recent: [], minutes: [] };
        }
        if (!resp.ok) return { error: resp.status === 401 ? "密钥不符" : `HTTP ${resp.status}`, latency };
        return { latency, ...(await resp.json()) };
      } catch (err) {
        return { error: err && err.name === "TimeoutError" ? "超时" : String((err && err.message) || err) };
      }
    };
    const all = Object.keys(this.hosts);
    const results = await Promise.all(all.map(one));
    const describe = (s) => ({ kind: this.cnbSlots.has(s) ? "cnb" : "server", name: this.names[s] || null, disabled: this.disabled.has(s) });
    return { fetched_at: Date.now() / 1000, slots: Object.fromEntries(all.map((s, i) => [s, { ...describe(s), ...results[i] }])) };
  }
}

// 所有 worker 都满了或不可用时立即触发一次轮换器(按负载扩容),不等定时任务。全局 3 分钟内最多一次
async function requestScaleUp(env) {
  if (!env.CNB_TOKEN || !env.CNB_REPO || !env.DB || Date.now() - lastScaleTrigger < 60000) return;
  lastScaleTrigger = Date.now();
  const t = now();
  const claimed = await env.DB.prepare(
    `INSERT INTO options (key, value) VALUES ('scale_trigger_at', ?1)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value WHERE CAST(options.value AS INTEGER) < ?2 RETURNING value`,
  ).bind(String(t), t - 180).first();
  if (!claimed) return;
  const resp = await fetch(`https://api.cnb.cool/${env.CNB_REPO}/-/build/start`, {
    method: "POST",
    headers: { authorization: `Bearer ${env.CNB_TOKEN}`, "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ branch: "main", event: "api_trigger_rotate", title: "扩容:worker 全部繁忙", sync: "false" }),
  });
  console.log("scale-up trigger", resp.status);
}

// ------------------------------------------------------------------ 计费路径
function bearerKey(request) {
  const key = request.headers.get("x-api-key");
  if (key) return key;
  const auth = request.headers.get("authorization") || "";
  return auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
}

async function billedCreateTask(rt, payload, auth, root) {
  const type = String((payload.task && payload.task.type) || "");
  if (PROXY_TASK_TYPES.has(type.toLowerCase())) return taskError("ERROR_TASK_NOT_SUPPORTED", `不支持带代理的 ${type}，请使用 TurnstileTaskProxyless`);
  const task = payload.task || {};
  const bad = badTarget(rt, task.websiteURL || task.websiteUrl, task.websiteKey);
  if (bad) return taskError("ERROR_INVALID_TASK_DATA", bad);
  const cost = intOption(await getOptions(rt.env), "price_turnstile");
  const denied = await reserve(rt.env, auth, cost);
  if (denied) return taskError("ERROR_ZERO_BALANCE", denied);
  const resp = await rt.createTask(JSON.stringify({ ...payload, clientKey: root }));
  const text = await resp.text();
  const data = parseJson(text);
  if (!data || data.errorId !== 0 || !data.taskId) {
    await refund(rt.env, auth.user.id, auth.token.id, cost);
    return new Response(text, { status: resp.status, headers: resp.headers });
  }
  try {
    await rt.env.DB.prepare("INSERT INTO tasks (id, user_id, token_id, cost, status, host, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)")
      .bind(String(data.taskId), auth.user.id, auth.token.id, cost, hostname(payload.task && payload.task.websiteURL), now()).run();
  } catch (err) {
    await refund(rt.env, auth.user.id, auth.token.id, cost);
    return taskError("ERROR_SERVICE_UNAVALIABLE", "任务记录失败，请重试");
  }
  return new Response(text, { status: resp.status, headers: resp.headers });
}

async function billedGetTaskResult(rt, payload, auth, root) {
  const taskId = String(payload.taskId || "");
  const task = taskId && (await rt.env.DB.prepare("SELECT * FROM tasks WHERE id = ?").bind(taskId).first());
  if (!task || task.user_id !== auth.user.id) return taskError("ERROR_TASKID_INVALID", "taskId 无效");
  const resp = await rt.getTaskResult(JSON.stringify({ clientKey: root, taskId }), taskId);
  const text = await resp.text();
  const data = parseJson(text);
  if (task.status === 0 && data) {
    if (data.errorId === 0 && data.status === "ready") {
      const done = await rt.env.DB.prepare("UPDATE tasks SET status = 1, finished_at = ? WHERE id = ? AND status = 0 RETURNING created_at")
        .bind(now(), taskId).first();
      if (done) {
        rt.ctx.waitUntil((async () => {
          const token = await rt.env.DB.prepare("SELECT name FROM tokens WHERE id = ?").bind(task.token_id).first();
          await settle(rt.env, {
            userId: task.user_id, username: auth.user.username, tokenId: task.token_id, tokenName: token ? token.name : "",
            cost: task.cost, content: "Turnstile", host: task.host, elapsed: now() - done.created_at, taskId,
          });
        })());
      }
    } else if (data.errorId) {
      const done = await rt.env.DB.prepare("UPDATE tasks SET status = 2, finished_at = ? WHERE id = ? AND status = 0 RETURNING id")
        .bind(now(), taskId).first();
      if (done) {
        await refund(rt.env, task.user_id, task.token_id, task.cost);
        rt.ctx.waitUntil(addLog(rt.env, {
          userId: task.user_id, username: auth.user.username, tokenName: auth.token.name, type: LOG.ERROR,
          content: `${data.errorCode}: ${String(data.errorDescription || "").slice(0, 160)}`, host: task.host, taskId,
        }));
      }
    }
  }
  return new Response(text, { status: resp.status, headers: resp.headers });
}

// 同步接口(/solve):预扣,成功结算,失败退还
async function billedSync(rt, body, auth, root, { cost, content, host, elapsedOf, succeeded }) {
  const denied = await reserve(rt.env, auth, cost);
  if (denied) return error(402, "insufficient_quota", denied);
  let resp;
  try {
    resp = await rt.any(body, root);
  } catch (err) {
    await refund(rt.env, auth.user.id, auth.token.id, cost);
    throw err;
  }
  const text = await resp.text();
  const data = parseJson(text);
  if (resp.status === 200 && data && succeeded(data)) {
    rt.ctx.waitUntil(settle(rt.env, {
      userId: auth.user.id, username: auth.user.username, tokenId: auth.token.id, tokenName: auth.token.name,
      cost, content, host, elapsed: elapsedOf(data),
    }));
  } else {
    await refund(rt.env, auth.user.id, auth.token.id, cost);
    rt.ctx.waitUntil(addLog(rt.env, {
      userId: auth.user.id, username: auth.user.username, tokenName: auth.token.name, type: LOG.ERROR,
      content: `${content}: ${(data && (data.code || data.message)) || `HTTP ${resp.status}`}`.slice(0, 200), host,
    }));
  }
  return new Response(text, { status: resp.status, headers: resp.headers });
}

async function relayBilled(rt, root, body) {
  const { request, url, env } = rt;
  const path = url.pathname;
  if (request.method === "POST" && (path === "/createTask" || path === "/getTaskResult" || path === "/getBalance")) {
    const payload = parseJson(body);
    if (!payload) return taskError(path === "/getTaskResult" ? "ERROR_TASKID_INVALID" : "ERROR_INVALID_TASK_DATA", "请求体不是合法的 JSON");
    const auth = await authenticate(env, payload.clientKey, root);
    if (auth && auth.root) {
      if (path === "/createTask") return rt.createTask(body);
      if (path === "/getTaskResult") return rt.getTaskResult(body, payload.taskId);
      return rt.any(body);
    }
    if (!auth) return taskError("ERROR_KEY_DOES_NOT_EXIST", "clientKey 错误");
    if (path === "/createTask") return billedCreateTask(rt, payload, auth, root);
    if (path === "/getTaskResult") return billedGetTaskResult(rt, payload, auth, root);
    const user = await env.DB.prepare("SELECT quota FROM users WHERE id = ?").bind(auth.user.id).first();
    return Response.json({ errorId: 0, errorCode: "", errorDescription: "", balance: user ? user.quota : 0 });
  }

  if (request.method === "POST" && path === "/solve") {
    const auth = await authenticate(env, bearerKey(request), root);
    if (auth && auth.root) return rt.any(body);
    if (!auth) return error(401, "unauthorized", "缺少或错误的 API Key");
    const payload = parseJson(body);
    if (!payload) return error(400, "invalid_request", "请求体不是合法的 JSON");
    if (payload.proxy != null && payload.proxy !== "") return error(422, "invalid_request", "不支持 proxy，Turnstile 由服务端直接求解，请去掉该字段");
    const bad = badTarget(rt, payload.url, payload.sitekey);
    if (bad) return error(422, "invalid_request", bad.replace("websiteKey", "sitekey").replace("websiteURL", "url"));
    return billedSync(rt, body, auth, root, {
      cost: intOption(await getOptions(env), "price_turnstile"),
      content: "Turnstile(同步)",
      host: hostname(payload.url),
      elapsedOf: (d) => d.elapsed,
      succeeded: (d) => Boolean(d.token),
    });
  }

  return rt.any(body);
}

// 未初始化(没有根密钥):与之前相同的纯分流
function relayPlain(rt, body) {
  const { request, url } = rt;
  if (request.method === "POST" && url.pathname === "/createTask") return rt.createTask(body);
  if (request.method === "POST" && url.pathname === "/getTaskResult") {
    const payload = parseJson(body);
    if (!payload) return taskError("ERROR_TASKID_INVALID", "请求体不是合法的 JSON");
    return rt.getTaskResult(body, payload.taskId);
  }
  return rt.any(body);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const rt = new Relay(request, env, ctx, url);
    await rt.init();

    // 控制台在站点根路径;不要求 HTML 的 GET /(API 客户端的就绪检查)照常转发给 worker,返回 {"msg": "Turnstile Solver is ready!"}
    const isGet = request.method === "GET" || request.method === "HEAD";
    if (url.pathname === "/" && isGet && (request.headers.get("accept") || "").includes("text/html")) {
      return new Response(request.method === "HEAD" ? null : ADMIN_HTML, { headers: ADMIN_PAGE_HEADERS });
    }
    if (url.pathname === "/admin" || url.pathname === "/admin/") {
      // 旧入口:浏览器跳转时保留 #页面(/admin#docs → /#docs)
      if (!isGet) return new Response(null, { status: 405 });
      return new Response(null, { status: 302, headers: { location: "/", "cache-control": "no-store" } });
    }
    // 5 秒盾已下线:不再转发,还没升级的 worker 也就不会再处理
    if (url.pathname === "/v1") return error(404, "not_found", "5 秒盾(/v1)已下线");
    // 轮换器读取各 worker 的负载(根密钥)
    if (url.pathname === "/api/fleet" && request.method === "GET") {
      const root = await rootKey(env);
      const key = bearerKey(request);
      if (!root || !key || !safeEqual(key, root)) return error(401, "unauthorized", "需要根密钥");
      return Response.json(await rt.fleet(root), { headers: { "cache-control": "no-store" } });
    }
    if (url.pathname.startsWith("/api/")) {
      return handleApi(request, env, url, {
        fleet: (key) => rt.fleet(key),
        validateSolverKey: (key) => rt.validateSolverKey(key),
      });
    }

    const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.text();
    const root = await rootKey(env);
    const resp = await (root ? relayBilled(rt, root, body) : relayPlain(rt, body));
    if (rt.saturated) ctx.waitUntil(requestScaleUp(env).catch((err) => console.error("scale-up trigger failed", err)));
    return resp;
  },

  async scheduled(event, env, ctx) {
    if (env.DB) ctx.waitUntil(sweep(env).catch((err) => console.error("sweep failed", err)));
  },
};
