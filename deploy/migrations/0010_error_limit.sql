-- 单用户单日单站点错误上限:只索引错误日志,统计时最多读上限 + 1 行
CREATE INDEX logs_errors ON logs(user_id, host, created_at) WHERE type = 5;
