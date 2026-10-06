-- GitHub 登录:账号绑定与 OAuth state(防止伪造回调与跨用户绑定)

ALTER TABLE users ADD COLUMN github_id TEXT;
ALTER TABLE users ADD COLUMN github_login TEXT;
CREATE UNIQUE INDEX users_github ON users(github_id) WHERE github_id IS NOT NULL;

CREATE TABLE oauth_states (
  state TEXT PRIMARY KEY,
  user_id INTEGER,                          -- 绑定流程:发起绑定的用户;登录流程为 NULL
  expires_at INTEGER NOT NULL
);
