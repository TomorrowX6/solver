-- 自有服务器渠道:每台服务器一条 Cloudflare 隧道与一个 solver-<位置>.<域名> 主机名
-- 位置 a–d 由 CNB 轮换器管理,服务器使用 e–p(taskId 第一位为位置序号,最多 16 个位置)

CREATE TABLE channels (
  slot TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  tunnel_id TEXT,
  dns_record_id TEXT,
  install_token TEXT NOT NULL UNIQUE,       -- 安装脚本用它下载配置与源码
  status INTEGER NOT NULL DEFAULT 1,        -- 1 启用 / 2 禁用(禁用后不再分配新任务)
  created_at INTEGER NOT NULL
);
