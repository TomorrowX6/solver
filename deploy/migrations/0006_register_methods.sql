-- 开放注册按方式分开:密码、GitHub、LINUX DO 各自一个开关,初始值沿用原来的 register_enabled

INSERT OR IGNORE INTO options (key, value) SELECT 'register_password_enabled', value FROM options WHERE key = 'register_enabled';
INSERT OR IGNORE INTO options (key, value) SELECT 'register_github_enabled', value FROM options WHERE key = 'register_enabled';
INSERT OR IGNORE INTO options (key, value) SELECT 'register_linuxdo_enabled', value FROM options WHERE key = 'register_enabled';
DELETE FROM options WHERE key = 'register_enabled';
