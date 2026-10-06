-- 服务器渠道的并发数:0 表示由安装脚本按 CPU 与内存自动计算
ALTER TABLE channels ADD COLUMN concurrency INTEGER NOT NULL DEFAULT 0;
