// 控制台接口(/api/*):初始化、登录注册、令牌、兑换码、日志、用户管理、系统设置。
// 响应格式参照 NewAPI:{success, message, data}。会话保存在 HttpOnly Cookie 中。

import {
  LOG, OPTION_DEFAULTS, PRIVATE_OPTIONS, ROLE, addLog, forgetToken, getOptions, hashPassword, intOption,
  now, randomString, rootKey, safeEqual, setOptions, sha256Hex, verifyPassword, forgetChannels,
} from "./db.js";
import { createTunnel, deleteTunnel, tunnelToken } from "./cloudflare.js";

const SESSION_COOKIE = "ts_session";
const OAUTH_COOKIE = "ts_oauth";
const SESSION_TTL = 7 * 24 * 3600;
const USERNAME_RE = /^[A-Za-z0-9_.-]{3,20}$/;
const MAX_TOKENS = 50;
const MAX_QUOTA = 1e12;

const json = (body, status = 200, headers = {}) =>
  Response.json(body, { status, headers: { "cache-control": "no-store", ...headers } });
const ok = (data = null, headers) => json({ success: true, message: "", data }, 200, headers);
const fail = (message, status = 200) => json({ success: false, message }, status);

class ApiError extends Error {}
const check = (cond, message) => {
  if (!cond) throw new ApiError(message);
};
const toInt = (v, def = 0) => (v === null || v === undefined || v === "" || !Number.isFinite(Number(v)) ? def : Math.trunc(Number(v)));

function publicUser(u) {
  return {
    id: u.id, username: u.username, role: u.role, status: u.status, quota: u.quota, used_quota: u.used_quota,
    request_count: u.request_count, created_at: u.created_at, last_login_at: u.last_login_at,
    github_login: u.github_login || null, linuxdo_login: u.linuxdo_login || null, has_password: Boolean(u.password_hash),
  };
}

function page(url) {
  const p = Math.max(1, toInt(url.searchParams.get("p"), 1));
  const size = Math.min(100, Math.max(1, toInt(url.searchParams.get("size"), 20)));
  return { p, size, offset: (p - 1) * size };
}

function cookie(request, name) {
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return "";
}

async function sessionUser(request, env) {
  const sid = cookie(request, SESSION_COOKIE);
  if (!sid) return null;
  const user = await env.DB.prepare(
    "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ? AND s.expires_at > ?",
  ).bind(await sha256Hex(sid), now()).first();
  return user && user.status === 1 ? user : null;
}

async function startSession(env, user) {
  const sid = randomString(43);
  const t = now();
  await env.DB.batch([
    env.DB.prepare("INSERT INTO sessions (id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)")
      .bind(await sha256Hex(sid), user.id, t + SESSION_TTL, t),
    env.DB.prepare("UPDATE users SET last_login_at = ? WHERE id = ?").bind(t, user.id),
  ]);
  return { "set-cookie": `${SESSION_COOKIE}=${sid}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_TTL}` };
}
const clearCookie = { "set-cookie": `${SESSION_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0` };

async function body(request) {
  try {
    const data = await request.json();
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}

function validCredentials(username, password) {
  check(typeof username === "string" && USERNAME_RE.test(username), "用户名为 3–20 位字母、数字或 _ . -");
  check(typeof password === "string" && password.length >= 8 && password.length <= 64, "密码长度为 8–64 位");
}

// ------------------------------------------------------------------ 公开接口
async function status(c) {
  const options = await getOptions(c.env);
  const users = await c.env.DB.prepare("SELECT COUNT(*) AS n FROM users").first();
  return ok({
    system_name: options.system_name,
    setup_required: users.n === 0,
    password_register: options.register_password_enabled === "true" && !oauthOnly(options),
    oauth_only: oauthOnly(options),
    price_turnstile: intOption(options, "price_turnstile"),
    price_v1: intOption(options, "price_v1"),
    github_oauth: OAUTH.github.enabled(options),
    linuxdo_oauth: OAUTH.linuxdo.enabled(options),
    v1_enabled: options.v1_enabled === "true",
  });
}

// 首次使用:用现有的 API Key 证明所有权,创建超级管理员
async function setup(c) {
  const { username, password, solver_key: key } = await body(c.request);
  validCredentials(username, password);
  check(typeof key === "string" && key.length >= 8, "请填写现有的 API Key");
  const exists = await c.env.DB.prepare("SELECT COUNT(*) AS n FROM users").first();
  check(exists.n === 0, "已初始化");
  if (c.env.SOLVER_KEY) {
    check(key === c.env.SOLVER_KEY, "API Key 错误");
  } else {
    check(await c.deps.validateSolverKey(key), "API Key 错误或 worker 不可用");
    await setOptions(c.env, { solver_key: key });
  }
  const hash = await hashPassword(password);
  const user = await c.env.DB.prepare(
    `INSERT INTO users (username, password_hash, role, status, quota, created_at)
     SELECT ?, ?, ?, 1, 0, ? WHERE NOT EXISTS (SELECT 1 FROM users) RETURNING *`,
  ).bind(username, hash, ROLE.ROOT, now()).first();
  check(user, "已初始化");
  await addLog(c.env, { userId: user.id, username, type: LOG.SYSTEM, content: "初始化系统" });
  return ok(publicUser(user), await startSession(c.env, user));
}

async function login(c) {
  const { username, password } = await body(c.request);
  check(typeof username === "string" && typeof password === "string", "请填写用户名和密码");
  if (oauthOnly(await getOptions(c.env))) return fail("已关闭密码登录，请使用第三方账号登录");
  const user = await c.env.DB.prepare("SELECT * FROM users WHERE username = ?").bind(username).first();
  if (!user) {
    await hashPassword(password); // 与存在的用户耗时一致
    return fail("用户名或密码错误");
  }
  if (!user.password_hash) return fail(`该账号使用 ${oauthName(user)} 登录`);
  if (!(await verifyPassword(password, user.password_hash))) return fail("用户名或密码错误");
  if (user.status !== 1) return fail("账号已禁用");
  return ok(publicUser(user), await startSession(c.env, user));
}

async function register(c) {
  const options = await getOptions(c.env);
  check(options.register_password_enabled === "true" && !oauthOnly(options), "未开放注册");
  const { username, password } = await body(c.request);
  validCredentials(username, password);
  const taken = await c.env.DB.prepare("SELECT 1 FROM users WHERE username = ?").bind(username).first();
  check(!taken, "用户名已存在");
  const quota = intOption(options, "new_user_quota");
  const user = await c.env.DB.prepare(
    "INSERT INTO users (username, password_hash, role, status, quota, created_at) VALUES (?, ?, ?, 1, ?, ?) RETURNING *",
  ).bind(username, await hashPassword(password), ROLE.USER, quota, now()).first();
  await addLog(c.env, { userId: user.id, username, type: LOG.SYSTEM, content: quota ? `注册赠送 ${quota} 积分` : "注册", quota });
  return ok(publicUser(user), await startSession(c.env, user));
}

async function logout(c) {
  const sid = cookie(c.request, SESSION_COOKIE);
  if (sid) await c.env.DB.prepare("DELETE FROM sessions WHERE id = ?").bind(await sha256Hex(sid)).run();
  return ok(null, clearCookie);
}

// ------------------------------------------------------------------ 第三方登录(GitHub、LINUX DO)
// 流程:/api/oauth/<方式> 生成 state(存 D1,并写入 SameSite=Lax 的 Cookie)后跳到授权页;
// 回调时 state 必须与 Cookie 一致、未过期且属于同一登录方式,防止伪造登录。绑定流程把发起者记在 state 里,
// 回调时不依赖会话 Cookie(会话 Cookie 是 SameSite=Strict,从授权页跳回时不会携带)。
// 每种方式在 users 表上有 <key>_id 与 <key>_login 两列(key 只取自下表,不来自请求)。
const callbackUrl = (c, key) => `${c.url.origin}/api/oauth/${key}/callback`;

const OAUTH = {
  github: {
    name: "GitHub",
    prefix: "gh",
    enabled: (o) => o.github_oauth_enabled === "true" && Boolean(o.github_client_id && o.github_client_secret),
    authorizeUrl(c, o, state) {
      const url = new URL("/login/oauth/authorize", c.env.GITHUB_BASE || "https://github.com");
      url.searchParams.set("client_id", o.github_client_id);
      url.searchParams.set("redirect_uri", callbackUrl(c, "github"));
      url.searchParams.set("scope", "read:user");
      url.searchParams.set("state", state);
      return url;
    },
    async fetchUser(c, o, code) {
      const ua = { "user-agent": "turnstile-solver" };
      const tokenResp = await fetch(new URL("/login/oauth/access_token", c.env.GITHUB_BASE || "https://github.com"), {
        method: "POST",
        headers: { ...ua, accept: "application/json", "content-type": "application/json" },
        body: JSON.stringify({
          client_id: o.github_client_id, client_secret: o.github_client_secret, code, redirect_uri: callbackUrl(c, "github"),
        }),
      });
      const token = await tokenResp.json().catch(() => ({}));
      if (!token.access_token) return null;
      const userResp = await fetch(new URL("/user", c.env.GITHUB_API || "https://api.github.com"), {
        headers: { ...ua, accept: "application/vnd.github+json", authorization: `Bearer ${token.access_token}` },
      });
      const gh = await userResp.json().catch(() => ({}));
      return gh && gh.id ? { id: String(gh.id), login: String(gh.login || gh.id) } : null;
    },
  },
  // https://connect.linux.do/.well-known/openid-configuration;最低信任等级在 Connect 的应用设置里限制
  linuxdo: {
    name: "LINUX DO",
    prefix: "ld",
    enabled: (o) => o.linuxdo_oauth_enabled === "true" && Boolean(o.linuxdo_client_id && o.linuxdo_client_secret),
    authorizeUrl(c, o, state) {
      const url = new URL("/oauth2/authorize", c.env.LINUXDO_BASE || "https://connect.linux.do");
      url.searchParams.set("response_type", "code");
      url.searchParams.set("client_id", o.linuxdo_client_id);
      url.searchParams.set("redirect_uri", callbackUrl(c, "linuxdo"));
      url.searchParams.set("state", state);
      return url;
    },
    async fetchUser(c, o, code) {
      const base = c.env.LINUXDO_BASE || "https://connect.linux.do";
      const tokenResp = await fetch(new URL("/oauth2/token", base), {
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/x-www-form-urlencoded",
          authorization: `Basic ${btoa(`${o.linuxdo_client_id}:${o.linuxdo_client_secret}`)}`,
        },
        body: new URLSearchParams({ grant_type: "authorization_code", code, redirect_uri: callbackUrl(c, "linuxdo") }),
      });
      const token = await tokenResp.json().catch(() => ({}));
      if (!token.access_token) return null;
      const userResp = await fetch(new URL("/api/user", base), {
        headers: { accept: "application/json", authorization: `Bearer ${token.access_token}` },
      });
      const ld = await userResp.json().catch(() => ({}));
      return ld && ld.id ? { id: String(ld.id), login: String(ld.username || ld.id) } : null;
    },
  },
};
// 仅第三方登录:开关打开且至少启用了一种第三方登录时才生效(都没启用时退回密码登录,避免所有人都登不上)
const oauthOnly = (o) => o.oauth_only_enabled === "true" && Object.values(OAUTH).some((p) => p.enabled(o));
// 该用户能否用第三方登录:绑定了至少一种已启用的方式
const canOauthLogin = (u, o) => Object.entries(OAUTH).some(([key, p]) => p.enabled(o) && u[`${key}_id`]);
const oauthName = (u) => (Object.entries(OAUTH).find(([key]) => u[`${key}_id`]) || [null, { name: "第三方账号" }])[1].name;

function htmlRedirect(target, cookies = []) {
  // 用页面内跳转而不是 302:由本站页面发起的导航会带上 SameSite=Strict 的会话 Cookie
  const headers = new Headers({
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'",
    "referrer-policy": "no-referrer",
  });
  for (const ck of cookies) headers.append("set-cookie", ck);
  const href = target.replace(/[^A-Za-z0-9/#?=&%._~-]/g, (ch) => encodeURIComponent(ch));
  return new Response(`<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=${href}"><a href="${href}">继续</a>`, { headers });
}
const clearOauthCookie = `${OAUTH_COOKIE}=; Path=/api/oauth; HttpOnly; Secure; SameSite=Lax; Max-Age=0`;
const oauthError = (message) => htmlRedirect(`/admin#oauth_error=${encodeURIComponent(message)}`, [clearOauthCookie]);

const oauthStart = (key) => async (c) => {
  const p = OAUTH[key];
  const options = await getOptions(c.env);
  if (!p.enabled(options)) return oauthError(`未启用 ${p.name} 登录`);
  let userId = null;
  if (c.url.searchParams.get("bind")) {
    const user = await sessionUser(c.request, c.env);
    if (!user) return oauthError("请先登录");
    userId = user.id;
  }
  const state = randomString(32);
  await c.env.DB.prepare("INSERT INTO oauth_states (state, user_id, provider, expires_at) VALUES (?, ?, ?, ?)")
    .bind(state, userId, key, now() + 600).run();
  return new Response(null, {
    status: 302,
    headers: {
      location: p.authorizeUrl(c, options, state).toString(),
      "cache-control": "no-store",
      "set-cookie": `${OAUTH_COOKIE}=${state}; Path=/api/oauth; HttpOnly; Secure; SameSite=Lax; Max-Age=600`,
    },
  });
};

async function uniqueUsername(env, login, prefix) {
  let base = login.replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 20);
  if (base.length < 3) base = `${prefix}_${base}`.slice(0, 20);
  for (let i = 0; i < 5; i++) {
    const name = i === 0 ? base : `${base.slice(0, 15)}_${randomString(4).toLowerCase()}`;
    if (!(await env.DB.prepare("SELECT 1 FROM users WHERE username = ?").bind(name).first())) return name;
  }
  return `${prefix}_${randomString(12).toLowerCase()}`;
}

const oauthCallback = (key) => async (c) => {
  const p = OAUTH[key];
  const q = c.url.searchParams;
  if (q.get("error")) return oauthError(`已取消 ${p.name} 授权`);
  const state = q.get("state") || "";
  const cookieState = cookie(c.request, OAUTH_COOKIE);
  if (!state || !cookieState || !safeEqual(state, cookieState)) return oauthError("登录已失效，请重试");
  const pending = await c.env.DB.prepare("DELETE FROM oauth_states WHERE state = ? AND expires_at > ? RETURNING user_id, provider")
    .bind(state, now()).first();
  if (!pending || (pending.provider || "github") !== key) return oauthError("登录已失效，请重试");
  const options = await getOptions(c.env);
  if (!p.enabled(options)) return oauthError(`未启用 ${p.name} 登录`);
  let account = null;
  try {
    account = await p.fetchUser(c, options, q.get("code") || "");
  } catch (err) {
    console.error(`${key} oauth`, err);
    return oauthError(`连接 ${p.name} 失败，请重试`);
  }
  if (!account) return oauthError(`${p.name} 授权失败，请重试`);

  const idCol = `${key}_id`;
  const loginCol = `${key}_login`;
  const owner = await c.env.DB.prepare(`SELECT * FROM users WHERE ${idCol} = ?`).bind(account.id).first();
  if (pending.user_id) {
    // 绑定到发起绑定的账号
    if (owner && owner.id !== pending.user_id) return oauthError(`该 ${p.name} 账号已绑定其他用户`);
    await c.env.DB.prepare(`UPDATE users SET ${idCol} = ?, ${loginCol} = ? WHERE id = ?`).bind(account.id, account.login, pending.user_id).run();
    return htmlRedirect("/admin#personal", [clearOauthCookie]);
  }

  let user = owner;
  if (!user) {
    if (options[`register_${key}_enabled`] !== "true") return oauthError(`该 ${p.name} 账号未绑定用户：请用密码登录后在个人设置中绑定`);
    const quota = intOption(options, "new_user_quota");
    user = await c.env.DB.prepare(
      `INSERT INTO users (username, password_hash, role, status, quota, created_at, ${idCol}, ${loginCol})
       VALUES (?, '', ?, 1, ?, ?, ?, ?) RETURNING *`,
    ).bind(await uniqueUsername(c.env, account.login, p.prefix), ROLE.USER, quota, now(), account.id, account.login).first();
    await addLog(c.env, {
      userId: user.id, username: user.username, type: LOG.SYSTEM, quota,
      content: quota ? `${p.name} 注册赠送 ${quota} 积分` : `${p.name} 注册`,
    });
  } else if (user[loginCol] !== account.login) {
    await c.env.DB.prepare(`UPDATE users SET ${loginCol} = ? WHERE id = ?`).bind(account.login, user.id).run();
  }
  if (user.status !== 1) return oauthError("账号已禁用");
  const session = await startSession(c.env, user);
  return htmlRedirect("/admin#dashboard", [session["set-cookie"], clearOauthCookie]);
};

// ------------------------------------------------------------------ 个人
async function self(c) {
  return ok(publicUser(c.user));
}

async function changePassword(c) {
  const { old_password: oldPassword, password } = await body(c.request);
  // 通过第三方登录创建的账号没有密码,首次设置不需要原密码
  if (c.user.password_hash) check(await verifyPassword(String(oldPassword || ""), c.user.password_hash), "原密码错误");
  validCredentials(c.user.username, password);
  await c.env.DB.batch([
    c.env.DB.prepare("UPDATE users SET password_hash = ? WHERE id = ?").bind(await hashPassword(password), c.user.id),
    c.env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(c.user.id),
  ]);
  return ok(null, await startSession(c.env, c.user));
}

async function dashboard(c) {
  const all = c.url.searchParams.get("scope") === "all" && c.user.role >= ROLE.ADMIN;
  const since = now() - 24 * 3600;
  const filter = all ? "" : "AND user_id = ?";
  const bind = (stmt, ...args) => (all ? stmt.bind(...args) : stmt.bind(...args, c.user.id));
  const [account, day, failed, hourly] = await c.env.DB.batch([
    all
      ? c.env.DB.prepare("SELECT SUM(quota) AS quota, SUM(used_quota) AS used_quota, SUM(request_count) AS request_count, COUNT(*) AS users FROM users")
      : c.env.DB.prepare("SELECT quota, used_quota, request_count FROM users WHERE id = ?").bind(c.user.id),
    bind(c.env.DB.prepare(`SELECT COUNT(*) AS n, COALESCE(SUM(quota), 0) AS q, AVG(elapsed) AS elapsed FROM logs WHERE type = 2 AND created_at >= ? ${filter}`), since),
    bind(c.env.DB.prepare(`SELECT COUNT(*) AS n FROM logs WHERE type = 5 AND created_at >= ? ${filter}`), since),
    bind(c.env.DB.prepare(
      `SELECT (created_at / 3600) * 3600 AS h, SUM(type = 2) AS ok, SUM(type = 5) AS fail, COALESCE(SUM(CASE WHEN type = 2 THEN quota END), 0) AS q
         FROM logs WHERE type IN (2, 5) AND created_at >= ? ${filter} GROUP BY h ORDER BY h`), since),
  ]);
  const a = account.results[0] || {};
  return ok({
    scope: all ? "all" : "self",
    quota: a.quota || 0,
    used_quota: a.used_quota || 0,
    request_count: a.request_count || 0,
    users: a.users ?? null,
    day: { count: day.results[0].n, quota: day.results[0].q, elapsed: day.results[0].elapsed, failed: failed.results[0].n },
    hourly: hourly.results.map((r) => [r.h, r.ok, r.fail, r.q]),
  });
}

// ------------------------------------------------------------------ 令牌
function tokenFields(input, current = {}) {
  const out = {};
  if ("name" in input || !current.name) {
    const name = String(input.name ?? "").trim();
    check(name.length >= 1 && name.length <= 30, "名称为 1–30 个字符");
    out.name = name;
  }
  if ("status" in input) {
    check([1, 2].includes(toInt(input.status)), "状态无效");
    out.status = toInt(input.status);
  }
  if ("unlimited_quota" in input) out.unlimited_quota = input.unlimited_quota ? 1 : 0;
  if ("remain_quota" in input) {
    const q = toInt(input.remain_quota, -1);
    check(q >= 0 && q <= MAX_QUOTA, "额度无效");
    out.remain_quota = q;
  }
  if ("expired_at" in input) {
    const e = toInt(input.expired_at, -1);
    check(e === -1 || e > now(), "过期时间无效");
    out.expired_at = e;
  }
  return out;
}

async function listTokens(c) {
  const { p, size, offset } = page(c.url);
  const keyword = `%${(c.url.searchParams.get("keyword") || "").trim()}%`;
  const [rows, total] = await c.env.DB.batch([
    c.env.DB.prepare("SELECT * FROM tokens WHERE user_id = ? AND name LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?").bind(c.user.id, keyword, size, offset),
    c.env.DB.prepare("SELECT COUNT(*) AS n FROM tokens WHERE user_id = ? AND name LIKE ?").bind(c.user.id, keyword),
  ]);
  return ok({ items: rows.results, total: total.results[0].n, p, size });
}

async function createToken(c) {
  const fields = tokenFields(await body(c.request));
  const count = await c.env.DB.prepare("SELECT COUNT(*) AS n FROM tokens WHERE user_id = ?").bind(c.user.id).first();
  check(count.n < MAX_TOKENS, `最多 ${MAX_TOKENS} 个令牌`);
  const token = await c.env.DB.prepare(
    `INSERT INTO tokens (user_id, name, key, status, unlimited_quota, remain_quota, expired_at, created_at)
     VALUES (?, ?, ?, 1, ?, ?, ?, ?) RETURNING *`,
  ).bind(c.user.id, fields.name, `sk-${randomString(48)}`, fields.unlimited_quota ?? 1, fields.remain_quota ?? 0, fields.expired_at ?? -1, now()).first();
  return ok(token);
}

async function ownToken(c) {
  const token = await c.env.DB.prepare("SELECT * FROM tokens WHERE id = ? AND user_id = ?").bind(toInt(c.params.id), c.user.id).first();
  check(token, "令牌不存在");
  return token;
}

async function updateToken(c) {
  const token = await ownToken(c);
  const fields = tokenFields(await body(c.request), token);
  const keys = Object.keys(fields);
  if (keys.length) {
    await c.env.DB.prepare(`UPDATE tokens SET ${keys.map((k) => `${k} = ?`).join(", ")} WHERE id = ?`)
      .bind(...keys.map((k) => fields[k]), token.id).run();
  }
  forgetToken(token.key);
  return ok(await c.env.DB.prepare("SELECT * FROM tokens WHERE id = ?").bind(token.id).first());
}

async function deleteToken(c) {
  const token = await ownToken(c);
  await c.env.DB.prepare("DELETE FROM tokens WHERE id = ?").bind(token.id).run();
  forgetToken(token.key);
  return ok();
}

// ------------------------------------------------------------------ 钱包
async function topup(c) {
  const key = String((await body(c.request)).key || "").trim();
  check(key.length >= 8, "请输入兑换码");
  const code = await c.env.DB.prepare(
    "UPDATE redemptions SET status = 3, used_by = ?, redeemed_at = ? WHERE key = ? AND status = 1 RETURNING quota",
  ).bind(c.user.id, now(), key).first();
  check(code, "兑换码无效或已使用");
  const user = await c.env.DB.prepare("UPDATE users SET quota = quota + ? WHERE id = ? RETURNING quota").bind(code.quota, c.user.id).first();
  await addLog(c.env, { userId: c.user.id, username: c.user.username, type: LOG.TOPUP, content: `兑换码充值 ${code.quota} 积分`, quota: code.quota });
  return ok({ quota: code.quota, balance: user.quota });
}

// ------------------------------------------------------------------ 日志
async function listLogs(c) {
  const { p, size, offset } = page(c.url);
  const q = c.url.searchParams;
  const where = [];
  const args = [];
  if (q.get("scope") === "all" && c.user.role >= ROLE.ADMIN) {
    if (q.get("username")) { where.push("username = ?"); args.push(q.get("username").trim()); }
  } else {
    where.push("user_id = ?");
    args.push(c.user.id);
  }
  if (toInt(q.get("type"))) { where.push("type = ?"); args.push(toInt(q.get("type"))); }
  if (q.get("token_name")) { where.push("token_name = ?"); args.push(q.get("token_name").trim()); }
  const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const [rows, total] = await c.env.DB.batch([
    c.env.DB.prepare(`SELECT * FROM logs ${clause} ORDER BY id DESC LIMIT ? OFFSET ?`).bind(...args, size, offset),
    c.env.DB.prepare(`SELECT COUNT(*) AS n FROM logs ${clause}`).bind(...args),
  ]);
  return ok({ items: rows.results, total: total.results[0].n, p, size });
}

// ------------------------------------------------------------------ 管理:用户
async function listUsers(c) {
  const { p, size, offset } = page(c.url);
  const keyword = `%${(c.url.searchParams.get("keyword") || "").trim()}%`;
  const [rows, total] = await c.env.DB.batch([
    c.env.DB.prepare("SELECT * FROM users WHERE username LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?").bind(keyword, size, offset),
    c.env.DB.prepare("SELECT COUNT(*) AS n FROM users WHERE username LIKE ?").bind(keyword),
  ]);
  return ok({ items: rows.results.map(publicUser), total: total.results[0].n, p, size });
}

async function createUser(c) {
  const input = await body(c.request);
  validCredentials(input.username, input.password);
  const role = toInt(input.role, ROLE.USER);
  check(role === ROLE.USER || (role === ROLE.ADMIN && c.user.role === ROLE.ROOT), "无权设置该角色");
  const quota = toInt(input.quota, 0);
  check(quota >= 0 && quota <= MAX_QUOTA, "积分无效");
  const taken = await c.env.DB.prepare("SELECT 1 FROM users WHERE username = ?").bind(input.username).first();
  check(!taken, "用户名已存在");
  const user = await c.env.DB.prepare(
    "INSERT INTO users (username, password_hash, role, status, quota, created_at) VALUES (?, ?, ?, 1, ?, ?) RETURNING *",
  ).bind(input.username, await hashPassword(input.password), role, quota, now()).first();
  if (quota) {
    await addLog(c.env, { userId: user.id, username: user.username, type: LOG.MANAGE, content: `${c.user.username} 创建账号，积分 ${quota}`, quota });
  }
  return ok(publicUser(user));
}

async function managedUser(c) {
  const target = await c.env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(toInt(c.params.id)).first();
  check(target, "用户不存在");
  check(target.id === c.user.id || c.user.role > target.role, "无权操作该用户");
  return target;
}

async function updateUser(c) {
  const target = await managedUser(c);
  const input = await body(c.request);
  const stmts = [];
  if ("status" in input) {
    const s = toInt(input.status);
    check([1, 2].includes(s), "状态无效");
    check(!(s === 2 && (target.role === ROLE.ROOT || target.id === c.user.id)), "不能禁用该账号");
    stmts.push(c.env.DB.prepare("UPDATE users SET status = ? WHERE id = ?").bind(s, target.id));
    if (s === 2) stmts.push(c.env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(target.id));
  }
  if ("role" in input && toInt(input.role) !== target.role) {
    const r = toInt(input.role);
    check(c.user.role === ROLE.ROOT && target.id !== c.user.id && [ROLE.USER, ROLE.ADMIN].includes(r), "无权修改角色");
    stmts.push(c.env.DB.prepare("UPDATE users SET role = ? WHERE id = ?").bind(r, target.id));
  }
  if (input.password) {
    validCredentials(target.username, input.password);
    stmts.push(c.env.DB.prepare("UPDATE users SET password_hash = ? WHERE id = ?").bind(await hashPassword(input.password), target.id));
    stmts.push(c.env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(target.id));
  }
  let delta = 0;
  if ("quota" in input) {
    const q = toInt(input.quota, -1);
    check(q >= 0 && q <= MAX_QUOTA, "积分无效");
    delta = q - target.quota;
    // 以相对值更新,避免覆盖同时发生的扣费
    if (delta) stmts.push(c.env.DB.prepare("UPDATE users SET quota = quota + ? WHERE id = ?").bind(delta, target.id));
  }
  if (stmts.length) await c.env.DB.batch(stmts);
  if (delta) {
    await addLog(c.env, {
      userId: target.id, username: target.username, type: LOG.MANAGE,
      content: `${c.user.username} 调整积分 ${delta > 0 ? "+" : ""}${delta}`, quota: delta,
    });
  }
  forgetToken();
  return ok(publicUser(await c.env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(target.id).first()));
}

async function deleteUser(c) {
  const target = await managedUser(c);
  check(target.role !== ROLE.ROOT && target.id !== c.user.id, "不能删除该账号");
  await c.env.DB.batch([
    c.env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(target.id),
    c.env.DB.prepare("DELETE FROM tokens WHERE user_id = ?").bind(target.id),
    c.env.DB.prepare("DELETE FROM users WHERE id = ?").bind(target.id),
  ]);
  forgetToken();
  return ok();
}

// ------------------------------------------------------------------ 管理:兑换码
async function listRedemptions(c) {
  const { p, size, offset } = page(c.url);
  const keyword = `%${(c.url.searchParams.get("keyword") || "").trim()}%`;
  const [rows, total] = await c.env.DB.batch([
    c.env.DB.prepare(
      `SELECT r.*, u.username AS used_by_name FROM redemptions r LEFT JOIN users u ON u.id = r.used_by
        WHERE r.name LIKE ? ORDER BY r.id DESC LIMIT ? OFFSET ?`,
    ).bind(keyword, size, offset),
    c.env.DB.prepare("SELECT COUNT(*) AS n FROM redemptions WHERE name LIKE ?").bind(keyword),
  ]);
  return ok({ items: rows.results, total: total.results[0].n, p, size });
}

async function createRedemptions(c) {
  const input = await body(c.request);
  const name = String(input.name || "").trim();
  const quota = toInt(input.quota);
  const count = toInt(input.count, 1);
  check(name.length >= 1 && name.length <= 20, "名称为 1–20 个字符");
  check(quota >= 1 && quota <= MAX_QUOTA, "积分无效");
  check(count >= 1 && count <= 100, "数量为 1–100");
  const t = now();
  const keys = Array.from({ length: count }, () => randomString(32));
  const stmt = c.env.DB.prepare("INSERT INTO redemptions (name, key, quota, status, created_by, created_at) VALUES (?, ?, ?, 1, ?, ?)");
  await c.env.DB.batch(keys.map((k) => stmt.bind(name, k, quota, c.user.id, t)));
  return ok(keys);
}

async function updateRedemption(c) {
  const s = toInt((await body(c.request)).status);
  check([1, 2].includes(s), "状态无效");
  const row = await c.env.DB.prepare("UPDATE redemptions SET status = ? WHERE id = ? AND status IN (1, 2) RETURNING id")
    .bind(s, toInt(c.params.id)).first();
  check(row, "兑换码不存在或已使用");
  return ok();
}

async function deleteRedemption(c) {
  await c.env.DB.prepare("DELETE FROM redemptions WHERE id = ?").bind(toInt(c.params.id)).run();
  return ok();
}

// ------------------------------------------------------------------ 管理:设置与渠道
async function getOptionList(c) {
  const options = await getOptions(c.env);
  const visible = Object.fromEntries(Object.entries(options).filter(([k]) => !PRIVATE_OPTIONS.has(k)));
  visible.github_client_secret_set = Boolean(options.github_client_secret);
  visible.linuxdo_client_secret_set = Boolean(options.linuxdo_client_secret);
  visible.cf_api_token_set = Boolean(options.cf_api_token);
  return ok(visible);
}

async function updateOptions(c) {
  const input = await body(c.request);
  const values = {};
  for (const [k, v] of Object.entries(input)) {
    if (k === "github_client_secret" || k === "linuxdo_client_secret" || k === "cf_api_token") {
      // 只写:留空表示不修改
      if (String(v || "").trim()) values[k] = String(v).trim();
      continue;
    }
    check(k in OPTION_DEFAULTS && !PRIVATE_OPTIONS.has(k), `未知设置 ${k}`);
    if (k === "github_client_id" || k === "linuxdo_client_id") {
      values[k] = String(v || "").trim().slice(0, 100);
    } else if (k === "system_name") {
      const name = String(v).trim();
      check(name.length >= 1 && name.length <= 30, "系统名称为 1–30 个字符");
      values[k] = name;
    } else if (k.endsWith("_enabled")) {
      values[k] = v === true || v === "true" ? "true" : "false";
    } else {
      const n = toInt(v, -1);
      check(n >= 0 && n <= MAX_QUOTA, `${k} 无效`);
      values[k] = String(n);
    }
  }
  // 不允许把自己锁在外面:修改后仅第三方登录生效,而自己没有绑定任何已启用的方式
  const before = await getOptions(c.env);
  const lockedOut = (o) => oauthOnly(o) && !canOauthLogin(c.user, o);
  check(!lockedOut({ ...before, ...values }) || lockedOut(before), "保存后你将无法登录：仅第三方登录需要你先在个人设置中绑定一种已启用的第三方账号");
  if (Object.keys(values).length) await setOptions(c.env, values);
  return getOptionList(c);
}

async function fleet(c) {
  return ok(await c.deps.fleet(await rootKey(c.env)));
}

// ------------------------------------------------------------------ 管理:服务器渠道
// 位置 a–d 归 CNB 轮换器;服务器渠道使用 e–p
const SERVER_SLOTS = "efghijklmnop";

function cloudflareConfig(c, options) {
  return { token: options.cf_api_token, accountId: c.env.CF_ACCOUNT_ID, zone: c.env.CHANNEL_ZONE, api: c.env.CF_API_BASE };
}

function installCommand(c, token) {
  const script = c.env.INSTALL_SCRIPT_URL || "https://raw.githubusercontent.com/TomorrowX6/solver/main/install.sh";
  return `bash <(curl -fsSL ${script}) -e ${c.url.origin} -t ${token}`;
}

async function listChannels(c) {
  const options = await getOptions(c.env);
  const cfg = cloudflareConfig(c, options);
  const { results } = await c.env.DB.prepare("SELECT slot, name, host, status, concurrency, install_token, created_at FROM channels ORDER BY slot").all();
  return ok({
    cf_ready: Boolean(cfg.token && cfg.accountId && cfg.zone),
    items: results.map(({ install_token: token, ...ch }) => ({ ...ch, install_command: installCommand(c, token) })),
  });
}

// 并发数:0 表示自动(安装脚本按 CPU 与内存计算)
function channelConcurrency(value) {
  const n = toInt(value, 0);
  check(n >= 0 && n <= 64, "并发数为 1–64,留空表示自动");
  return n;
}

async function createChannel(c) {
  const input = await body(c.request);
  const name = String(input.name || "").trim();
  check(name.length >= 1 && name.length <= 30, "名称为 1–30 个字符");
  const concurrency = channelConcurrency(input.concurrency);
  const cfg = cloudflareConfig(c, await getOptions(c.env));
  check(cfg.token && cfg.accountId && cfg.zone, "请先在「系统设置」填写 Cloudflare API Token");
  const { results } = await c.env.DB.prepare("SELECT slot FROM channels").all();
  const used = new Set(results.map((r) => r.slot));
  const slot = [...SERVER_SLOTS].find((x) => !used.has(x));
  check(slot, "服务器渠道已满(最多 12 个)");
  let tunnel;
  try {
    tunnel = await createTunnel(cfg, slot, randomString(6).toLowerCase());
  } catch (err) {
    throw new ApiError(String(err.message || err));
  }
  const token = `ch-${randomString(40)}`;
  try {
    await c.env.DB.prepare(
      "INSERT INTO channels (slot, name, host, tunnel_id, dns_record_id, install_token, status, concurrency, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
    ).bind(slot, name, tunnel.host, tunnel.tunnelId, tunnel.dnsRecordId, token, concurrency, now()).run();
  } catch (err) {
    await deleteTunnel(cfg, tunnel.tunnelId, tunnel.dnsRecordId).catch(() => {});
    throw err;
  }
  forgetChannels();
  return ok({ slot, name, host: tunnel.host, status: 1, concurrency, install_command: installCommand(c, token) });
}

async function channelBySlot(c) {
  const ch = await c.env.DB.prepare("SELECT * FROM channels WHERE slot = ?").bind(String(c.params.slot)).first();
  check(ch, "渠道不存在");
  return ch;
}

async function updateChannel(c) {
  const ch = await channelBySlot(c);
  const input = await body(c.request);
  const name = "name" in input ? String(input.name || "").trim() : ch.name;
  check(name.length >= 1 && name.length <= 30, "名称为 1–30 个字符");
  const status = "status" in input ? toInt(input.status) : ch.status;
  check([1, 2].includes(status), "状态无效");
  const concurrency = "concurrency" in input ? channelConcurrency(input.concurrency) : ch.concurrency;
  await c.env.DB.prepare("UPDATE channels SET name = ?, status = ?, concurrency = ? WHERE slot = ?").bind(name, status, concurrency, ch.slot).run();
  forgetChannels();
  return ok();
}

async function resetChannelToken(c) {
  const ch = await channelBySlot(c);
  const token = `ch-${randomString(40)}`;
  await c.env.DB.prepare("UPDATE channels SET install_token = ? WHERE slot = ?").bind(token, ch.slot).run();
  return ok({ install_command: installCommand(c, token) });
}

async function deleteChannel(c) {
  const ch = await channelBySlot(c);
  const cfg = cloudflareConfig(c, await getOptions(c.env));
  let warning = "";
  try {
    if (!cfg.token) throw new Error("未配置 Cloudflare API Token");
    await deleteTunnel(cfg, ch.tunnel_id, ch.dns_record_id);
  } catch (err) {
    warning = `渠道已删除，但 Cloudflare 隧道或 DNS 记录未能清理：${String(err.message || err)}`;
  }
  await c.env.DB.prepare("DELETE FROM channels WHERE slot = ?").bind(ch.slot).run();
  forgetChannels();
  return ok({ warning });
}

// 安装脚本用安装令牌下载 worker 配置(含隧道令牌与 API Key)
async function channelConfig(c) {
  const auth = c.request.headers.get("authorization") || "";
  const token = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
  const text = (body, status = 200) => new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });
  const ch = token && (await c.env.DB.prepare("SELECT * FROM channels WHERE install_token = ?").bind(token).first());
  if (!ch) return text("安装令牌无效或渠道已删除\n", 401);
  const root = await rootKey(c.env);
  if (!root) return text("系统尚未初始化\n", 503);
  const cfg = cloudflareConfig(c, await getOptions(c.env));
  let tunnel;
  try {
    tunnel = await tunnelToken(cfg, ch.tunnel_id);
  } catch (err) {
    return text(`获取隧道令牌失败:${String(err.message || err)}\n`, 502);
  }
  const lines = [
    `FLEET_SLOT=${ch.slot}`,
    `TUNNEL_TOKEN_${ch.slot.toUpperCase()}=${tunnel}`,
    `TS_API_KEY=${root}`,
    "AGENT_PERMANENT=1",
    "TS_ATTEMPT_TIMEOUT=35",
    "TS_MAX_TIMEOUT=85",
    "TS_STALL_SECONDS=20",
    "TS_TASK_TTL=300",
    "TS_MAX_PENDING_TASKS=60",
    "TS_MAX_SESSIONS=4",
    "TS_SESSION_IDLE_TTL=1800",
  ];
  if (ch.concurrency > 0) lines.push(`TS_MAX_CONCURRENCY=${ch.concurrency}`);
  return text(lines.join("\n") + "\n");
}

// ------------------------------------------------------------------ 路由
const ROUTES = [
  ["GET", "/api/status", status, 0],
  ["POST", "/api/setup", setup, 0],
  ["POST", "/api/user/login", login, 0],
  ["POST", "/api/user/register", register, 0],
  ["POST", "/api/user/logout", logout, 0],
  ...Object.keys(OAUTH).flatMap((key) => [
    ["GET", `/api/oauth/${key}`, oauthStart(key), 0],
    ["GET", `/api/oauth/${key}/callback`, oauthCallback(key), 0],
  ]),
  ["GET", "/api/user/self", self, ROLE.USER],
  ["PUT", "/api/user/password", changePassword, ROLE.USER],
  ["GET", "/api/dashboard", dashboard, ROLE.USER],
  ["GET", "/api/token", listTokens, ROLE.USER],
  ["POST", "/api/token", createToken, ROLE.USER],
  ["PUT", "/api/token/:id", updateToken, ROLE.USER],
  ["DELETE", "/api/token/:id", deleteToken, ROLE.USER],
  ["POST", "/api/user/topup", topup, ROLE.USER],
  ["GET", "/api/log", listLogs, ROLE.USER],
  ["GET", "/api/admin/user", listUsers, ROLE.ADMIN],
  ["POST", "/api/admin/user", createUser, ROLE.ADMIN],
  ["PUT", "/api/admin/user/:id", updateUser, ROLE.ADMIN],
  ["DELETE", "/api/admin/user/:id", deleteUser, ROLE.ADMIN],
  ["GET", "/api/admin/redemption", listRedemptions, ROLE.ADMIN],
  ["POST", "/api/admin/redemption", createRedemptions, ROLE.ADMIN],
  ["PUT", "/api/admin/redemption/:id", updateRedemption, ROLE.ADMIN],
  ["DELETE", "/api/admin/redemption/:id", deleteRedemption, ROLE.ADMIN],
  ["GET", "/api/admin/option", getOptionList, ROLE.ADMIN],
  ["PUT", "/api/admin/option", updateOptions, ROLE.ADMIN],
  ["GET", "/api/admin/fleet", fleet, ROLE.ADMIN],
  ["GET", "/api/admin/channels", listChannels, ROLE.ADMIN],
  ["POST", "/api/admin/channels", createChannel, ROLE.ADMIN],
  ["PUT", "/api/admin/channels/:slot", updateChannel, ROLE.ADMIN],
  ["POST", "/api/admin/channels/:slot/token", resetChannelToken, ROLE.ADMIN],
  ["DELETE", "/api/admin/channels/:slot", deleteChannel, ROLE.ADMIN],
  ["GET", "/api/channel/config", channelConfig, 0],
];

function match(pattern, path) {
  const a = pattern.split("/");
  const b = path.split("/");
  if (a.length !== b.length) return null;
  const params = {};
  for (let i = 0; i < a.length; i++) {
    if (a[i].startsWith(":")) params[a[i].slice(1)] = decodeURIComponent(b[i]);
    else if (a[i] !== b[i]) return null;
  }
  return params;
}

export async function handleApi(request, env, url, deps) {
  if (!env.DB) return fail("未绑定 D1 数据库", 503);
  // 跨站请求防护:写操作必须同源(会话 Cookie 另有 SameSite=Strict)
  if (request.method !== "GET") {
    const origin = request.headers.get("origin");
    if (origin && origin !== url.origin) return fail("跨站请求被拒绝", 403);
  }
  for (const [method, pattern, handler, minRole] of ROUTES) {
    if (method !== request.method) continue;
    const params = match(pattern, url.pathname);
    if (!params) continue;
    const c = { request, env, url, params, deps, user: null };
    try {
      if (minRole) {
        c.user = await sessionUser(request, env);
        if (!c.user) return fail("未登录", 401);
        if (c.user.role < minRole) return fail("无权访问", 403);
      }
      return await handler(c);
    } catch (err) {
      if (err instanceof ApiError) return fail(err.message);
      if (String(err).includes("no such table")) return fail("数据库未初始化:请先执行 D1 迁移", 503);
      console.error("api error", url.pathname, err);
      return fail("服务器错误", 500);
    }
  }
  return fail("接口不存在", 404);
}
