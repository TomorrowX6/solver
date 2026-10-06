-- LINUX DO 登录:账号绑定;OAuth state 记下发起的登录方式,回调时核对(旧记录为 NULL,按 GitHub 处理)

ALTER TABLE users ADD COLUMN linuxdo_id TEXT;
ALTER TABLE users ADD COLUMN linuxdo_login TEXT;
CREATE UNIQUE INDEX users_linuxdo ON users(linuxdo_id) WHERE linuxdo_id IS NOT NULL;

ALTER TABLE oauth_states ADD COLUMN provider TEXT;
