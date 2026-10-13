-- 「全部日志」页按站点、用户筛选
CREATE INDEX logs_host ON logs(host, created_at);
CREATE INDEX logs_username ON logs(username, created_at);
