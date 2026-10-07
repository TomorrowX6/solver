-- 5 秒盾(FlareSolverr /v1)下线:删除它的开关与价格
DELETE FROM options WHERE key IN ('v1_enabled', 'price_v1');
