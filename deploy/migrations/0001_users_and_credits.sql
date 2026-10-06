-- 用户、令牌、积分与日志(Cloudflare D1)
-- 应用:npx wrangler@4 d1 migrations apply solver-db --remote --config deploy/wrangler.toml

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,              -- pbkdf2$迭代次数$盐$哈希
  role INTEGER NOT NULL DEFAULT 1,          -- 1 用户 / 10 管理员 / 100 超级管理员
  status INTEGER NOT NULL DEFAULT 1,        -- 1 启用 / 2 禁用
  quota INTEGER NOT NULL DEFAULT 0,         -- 剩余积分
  used_quota INTEGER NOT NULL DEFAULT 0,
  request_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER
);

CREATE TABLE tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  status INTEGER NOT NULL DEFAULT 1,        -- 1 启用 / 2 禁用
  unlimited_quota INTEGER NOT NULL DEFAULT 1,
  remain_quota INTEGER NOT NULL DEFAULT 0,  -- unlimited_quota = 0 时生效
  used_quota INTEGER NOT NULL DEFAULT 0,
  expired_at INTEGER NOT NULL DEFAULT -1,   -- -1 永不过期
  created_at INTEGER NOT NULL,
  accessed_at INTEGER
);
CREATE INDEX tokens_user ON tokens(user_id);

CREATE TABLE redemptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  quota INTEGER NOT NULL,
  status INTEGER NOT NULL DEFAULT 1,        -- 1 未使用 / 2 禁用 / 3 已使用
  created_by INTEGER,
  used_by INTEGER,
  created_at INTEGER NOT NULL,
  redeemed_at INTEGER
);

-- 已预扣积分、等待结果的任务;成功时结算,失败或超时退还
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  token_id INTEGER NOT NULL,
  cost INTEGER NOT NULL,
  status INTEGER NOT NULL DEFAULT 0,        -- 0 进行中 / 1 成功 / 2 失败(已退还)
  host TEXT,
  created_at INTEGER NOT NULL,
  finished_at INTEGER
);
CREATE INDEX tasks_open ON tasks(status, created_at);

CREATE TABLE logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  token_name TEXT NOT NULL DEFAULT '',
  type INTEGER NOT NULL,                    -- 1 充值 / 2 消费 / 3 管理 / 4 系统 / 5 错误
  content TEXT NOT NULL DEFAULT '',
  quota INTEGER NOT NULL DEFAULT 0,
  host TEXT,
  elapsed REAL,
  task_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX logs_user ON logs(user_id, created_at);
CREATE INDEX logs_created ON logs(created_at);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,                      -- 会话令牌的 SHA-256
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE options (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
