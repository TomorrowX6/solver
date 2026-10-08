// D1 数据访问:密码、随机密钥、系统设置、令牌鉴权与积分结算。

export const ROLE = { USER: 1, ADMIN: 10, ROOT: 100 };
export const LOG = { TOPUP: 1, CONSUME: 2, MANAGE: 3, SYSTEM: 4, ERROR: 5 };

export const now = () => Math.floor(Date.now() / 1000);

const enc = new TextEncoder();
const toB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const fromB64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

export function safeEqual(a, b) {
  const x = typeof a === "string" ? enc.encode(a) : a;
  const y = typeof b === "string" ? enc.encode(b) : b;
  if (x.byteLength !== y.byteLength) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

const ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
export function randomString(length, alphabet = ALNUM) {
  const limit = 256 - (256 % alphabet.length); // 拒绝采样,避免取模偏差
  let out = "";
  while (out.length < length) {
    for (const b of crypto.getRandomValues(new Uint8Array(length * 2))) {
      if (b < limit && out.length < length) out += alphabet[b % alphabet.length];
    }
  }
  return out;
}

export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", enc.encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Workers 的 PBKDF2 最多支持 100000 次迭代
const PBKDF2_ITERATIONS = 100000;
async function pbkdf2(password, salt, iterations) {
  const key = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, key, 256));
}
export async function hashPassword(password) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  return `pbkdf2$${PBKDF2_ITERATIONS}$${toB64(salt)}$${toB64(await pbkdf2(password, salt, PBKDF2_ITERATIONS))}`;
}
export async function verifyPassword(password, stored) {
  const [scheme, iterations, salt, hash] = String(stored || "").split("$");
  if (scheme !== "pbkdf2" || !salt || !hash) return false;
  return safeEqual(await pbkdf2(password, fromB64(salt), Number(iterations)), fromB64(hash));
}

// ------------------------------------------------------------------ 系统设置
export const OPTION_DEFAULTS = {
  system_name: "Turnstile Solver",
  price_turnstile: "1", // 每次成功求解扣除的积分
  // 开放注册:按方式分别控制;关闭的方式不能创建新账号,已有账号仍可登录
  register_password_enabled: "false",
  register_github_enabled: "false",
  register_linuxdo_enabled: "false",
  oauth_only_enabled: "false", // 仅第三方登录:关闭密码登录与密码注册,登录页只显示 GitHub / LINUX DO
  new_user_quota: "0",
  max_users: "0", // 注册人数上限:用户总数(含管理员)达到后不能再注册,0 表示不限;管理员手动添加不受限制
  checkin_quota: "0", // 每日签到:每天首次登录或打开控制台时发放的积分,0 表示关闭
  solver_key: "", // worker 的 API Key(初始化时校验后保存;也可用 Worker 密钥 SOLVER_KEY 提供)
  github_oauth_enabled: "false",
  github_client_id: "",
  github_client_secret: "",
  linuxdo_oauth_enabled: "false",
  linuxdo_client_id: "",
  linuxdo_client_secret: "",
  cf_api_token: "", // 创建服务器渠道的隧道与 DNS 记录
};
// 不返回给前端的设置
export const PRIVATE_OPTIONS = new Set(["solver_key", "scale_trigger_at", "github_client_secret", "linuxdo_client_secret", "cf_api_token"]);

let optionCache = null;
let optionCachedAt = 0;
export async function getOptions(env) {
  if (optionCache && Date.now() - optionCachedAt < 10000) return optionCache;
  const { results } = await env.DB.prepare("SELECT key, value FROM options").all();
  optionCache = { ...OPTION_DEFAULTS, ...Object.fromEntries(results.map((r) => [r.key, r.value])) };
  optionCachedAt = Date.now();
  return optionCache;
}
export async function setOptions(env, values) {
  const stmt = env.DB.prepare("INSERT INTO options (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value");
  await env.DB.batch(Object.entries(values).map(([k, v]) => stmt.bind(k, String(v))));
  optionCache = null;
}
export const intOption = (options, key) => Math.max(0, parseInt(options[key], 10) || 0);

// ------------------------------------------------------------------ 服务器渠道
let channelCache = null;
let channelCachedAt = 0;
/** 服务器渠道列表(位置、主机名、状态),缓存 10 秒;表未建时返回空。 */
export async function getChannels(env) {
  if (channelCache && Date.now() - channelCachedAt < 10000) return channelCache;
  try {
    const { results } = await env.DB.prepare("SELECT slot, name, host, status FROM channels ORDER BY slot").all();
    channelCache = results;
  } catch {
    channelCache = [];
  }
  channelCachedAt = Date.now();
  return channelCache;
}
export function forgetChannels() {
  channelCache = null;
}

// 转发给 worker 时使用的 API Key;为空表示尚未初始化(用户系统未启用)
export async function rootKey(env) {
  if (env.SOLVER_KEY) return env.SOLVER_KEY;
  if (!env.DB) return "";
  try {
    return (await getOptions(env)).solver_key || "";
  } catch {
    return ""; // 尚未建表
  }
}

// ------------------------------------------------------------------ 令牌鉴权与积分
const tokenCache = new Map(); // key -> {at, value}
export function forgetToken(key) {
  if (key) tokenCache.delete(key);
  else tokenCache.clear();
}

/**
 * 返回 {root: true}、{token, user} 或 null。令牌状态缓存 15 秒;积分扣除始终直接读写数据库。
 */
export async function authenticate(env, key, root) {
  if (typeof key !== "string" || !key) return null;
  if (root && safeEqual(key, root)) return { root: true };
  const cached = tokenCache.get(key);
  if (cached && Date.now() - cached.at < 15000) return cached.value;
  const row = await env.DB.prepare(
    `SELECT t.id AS token_id, t.name AS token_name, t.status AS token_status, t.expired_at, t.unlimited_quota,
            u.id AS user_id, u.username, u.status AS user_status
       FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.key = ?`,
  ).bind(key).first();
  let value = null;
  if (row && row.token_status === 1 && row.user_status === 1 && (row.expired_at === -1 || row.expired_at > now())) {
    value = {
      token: { id: row.token_id, name: row.token_name, unlimited: row.unlimited_quota === 1 },
      user: { id: row.user_id, username: row.username },
    };
  }
  if (tokenCache.size > 2000) tokenCache.clear();
  tokenCache.set(key, { at: Date.now(), value });
  return value;
}

/** 预扣积分。成功返回 null,否则返回错误说明。 */
export async function reserve(env, auth, cost) {
  if (cost <= 0) return null;
  const user = await env.DB.prepare("UPDATE users SET quota = quota - ?1 WHERE id = ?2 AND status = 1 AND quota >= ?1 RETURNING quota")
    .bind(cost, auth.user.id).first();
  if (!user) return "积分不足";
  if (!auth.token.unlimited) {
    const token = await env.DB.prepare(
      "UPDATE tokens SET remain_quota = remain_quota - ?1 WHERE id = ?2 AND (unlimited_quota = 1 OR remain_quota >= ?1) RETURNING id",
    ).bind(cost, auth.token.id).first();
    if (!token) {
      await env.DB.prepare("UPDATE users SET quota = quota + ? WHERE id = ?").bind(cost, auth.user.id).run();
      return "令牌额度不足";
    }
  }
  return null;
}

/** 退还预扣的积分。 */
export async function refund(env, userId, tokenId, cost) {
  if (cost <= 0) return;
  await env.DB.batch([
    env.DB.prepare("UPDATE users SET quota = quota + ? WHERE id = ?").bind(cost, userId),
    env.DB.prepare("UPDATE tokens SET remain_quota = remain_quota + ? WHERE id = ? AND unlimited_quota = 0").bind(cost, tokenId),
  ]);
}

/** 成功后结算:累计用量并写消费日志(积分已在预扣时扣除)。 */
export async function settle(env, { userId, username, tokenId, tokenName, cost, content, host, elapsed, taskId }) {
  const t = now();
  await env.DB.batch([
    env.DB.prepare("UPDATE users SET used_quota = used_quota + ?, request_count = request_count + 1 WHERE id = ?").bind(cost, userId),
    env.DB.prepare("UPDATE tokens SET used_quota = used_quota + ?, accessed_at = ? WHERE id = ?").bind(cost, t, tokenId),
    env.DB.prepare(
      "INSERT INTO logs (user_id, username, token_name, type, content, quota, host, elapsed, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    ).bind(userId, username || "", tokenName || "", LOG.CONSUME, content, cost, host || null, elapsed ?? null, taskId || null, t),
  ]);
}

export async function addLog(env, { userId, username = "", tokenName = "", type, content, quota = 0, host = null, elapsed = null, taskId = null }) {
  await env.DB.prepare(
    "INSERT INTO logs (user_id, username, token_name, type, content, quota, host, elapsed, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(userId, username, tokenName, type, content, quota, host, elapsed, taskId, now()).run();
}

// 数据保留期限:已结束的任务记录 90 天,使用日志 365 天
const TASK_RETENTION = 90 * 24 * 3600;
const LOG_RETENTION = 365 * 24 * 3600;
const CLEANUP_BATCH = 5000; // 每轮每张表最多删除的行数,积压较多时分多轮完成

/** 定时任务:退还超过 10 分钟仍未结算的任务(客户端没有取结果),清理过期会话、旧任务记录与旧日志。 */
export async function sweep(env) {
  const cutoff = now() - 600;
  const { results } = await env.DB.prepare("SELECT id FROM tasks WHERE status = 0 AND created_at < ? LIMIT 200").bind(cutoff).all();
  for (const { id } of results) {
    const task = await env.DB.prepare("UPDATE tasks SET status = 2, finished_at = ? WHERE id = ? AND status = 0 RETURNING user_id, token_id, cost")
      .bind(now(), id).first();
    if (task) await refund(env, task.user_id, task.token_id, task.cost);
  }
  await env.DB.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(now()).run();
  await env.DB.prepare("DELETE FROM oauth_states WHERE expires_at < ?").bind(now()).run();
  await env.DB.batch([
    env.DB.prepare(
      "DELETE FROM tasks WHERE id IN (SELECT id FROM tasks WHERE status != 0 AND created_at < ? LIMIT ?)",
    ).bind(now() - TASK_RETENTION, CLEANUP_BATCH),
    env.DB.prepare(
      "DELETE FROM logs WHERE id IN (SELECT id FROM logs WHERE created_at < ? ORDER BY id LIMIT ?)",
    ).bind(now() - LOG_RETENTION, CLEANUP_BATCH),
  ]);
}
