# 部署:Cloudflare Worker 控制台 + 服务器渠道

## 架构

```
客户端 ──HTTPS──► solver.example.com ── Cloudflare Worker(cf-worker.js:控制台、计费、随机分流 + 失败改投)
                                           │   D1:用户、令牌、积分、日志、渠道
                     ┌─────────────────────┼─────────────────────┐
              solver-a.example.com  solver-b.example.com  …  solver-p.example.com   ← 每台服务器一条 Cloudflare 隧道
                     │                     │                     │
                 服务器 [a]             服务器 [b]            服务器 [p]
            每台:cloudflared + 网关(:8686) + FlareSolverr(:8191),由 fleet/agent.py 守护
```

- **服务器渠道**:控制台「渠道」页添加服务器时,Worker 用 Cloudflare API 建一条隧道和 `solver-<位置>.<CHANNEL_ZONE>`
  主机名,并生成安装命令;服务器上的 `install.sh` 用安装令牌从 `/api/channel/config` 取得位置、隧道令牌与 API Key,
  再以 Docker(镜像 `ghcr.io/tomorrowx6/solver`)或 systemd 服务(`--no-docker`)运行 `fleet/agent.py`。
  位置 a–p 最多 16 个(taskId 第一位是位置序号)。
- **分流**:Worker 随机打乱各台服务器的顺序依次尝试;某一台返回 429(槽位与排队都已满)、503(求解器不可用)、
  无可用连接器(530)或源站连接失败时,改投下一台。单次求解总超时上限 85 秒(`TS_MAX_TIMEOUT`),
  不超过 Cloudflare 100 秒的源站超时。
- **任务**:`createTask` 随机分到某台服务器,返回 `ERROR_NO_SLOT_AVAILABLE` 时 Worker 自动改投下一台;
  taskId 是 UUID 格式,第一位是位置编号(a=0、b=1……),后 7 位是 worker 标识,`getTaskResult` 按第一位路由回原服务器。
  服务器重启后,它名下尚未取走的结果随之丢失,查询返回 `ERROR_TASKID_INVALID`,重新创建即可。
- **计费**:用户令牌(`sk-…`)调用时预扣积分,求解成功才结算,失败或 10 分钟未取结果自动退还;根密钥不计费。

## 一次性配置

需要一个托管在 Cloudflare 上的域名(下文以 `example.com` 为例)和 Node.js(用 `npx wrangler@4`)。

### 1. 部署 Worker

1. 修改 [`wrangler.toml`](wrangler.toml):`account_id` 与 `CF_ACCOUNT_ID` 填你的账户 ID,`routes` 改成你的入口
   (例如 `solver.example.com/*`,`zone_name = "example.com"`),`CHANNEL_ZONE` 填服务器主机名所在的域名。
2. 创建数据库并建表,把 `d1 create` 输出的 `database_id` 填进 `wrangler.toml`:

   ```bash
   npx wrangler@4 login
   npx wrangler@4 d1 create solver-db
   npx wrangler@4 d1 migrations apply solver-db --remote --config deploy/wrangler.toml
   ```

3. 设置根密钥(转发给服务器的 API Key,也会下发给安装的服务器),然后部署:

   ```bash
   openssl rand -hex 24 | npx wrangler@4 secret put SOLVER_KEY --config deploy/wrangler.toml
   npx wrangler@4 deploy --config deploy/wrangler.toml
   ```

4. 路由要生效,入口主机名需要一条代理(橙色云朵)的 DNS 记录,例如 `AAAA solver 100::`。
   路由只匹配入口主机名:不要写成 `*.example.com/*`,那会拦截该域名下的所有站点。

### 2. 初始化与系统设置

打开 `https://solver.example.com`,用上一步的 `SOLVER_KEY` 初始化超级管理员,然后在「系统设置」中:

- **服务器渠道**:填写 Cloudflare API Token。在 dash.cloudflare.com → 我的个人资料 → API 令牌 创建,权限:
  账户 · Cloudflare Tunnel · 编辑;区域 · DNS · 编辑;区域 · 区域 · 读取(区域选 `CHANNEL_ZONE`)。
- **第三方登录**(可选):
  - GitHub:GitHub → Settings → Developer settings → OAuth Apps 新建应用,Homepage 填 `https://solver.example.com`,
    回调地址填 `https://solver.example.com/api/oauth/github/callback`。
  - LINUX DO:connect.linux.do → 应用接入 → 申请接入新建应用,应用主页填 `https://solver.example.com`,
    回调地址填 `https://solver.example.com/api/oauth/linuxdo/callback`,最低等级在那里设置。

  把 Client ID / Secret 填到「系统设置 → 第三方登录」。
- **基础设置、注册登录**:价格、注册开关、注册人数上限、新用户赠送、每日签到。

### 3. 添加服务器

「渠道」页点「添加服务器」,在服务器上以 root 执行给出的安装命令(Docker 版或 `--no-docker` 版,说明见主
[README](../README.md#自有服务器渠道))。脚本等自检通过后退出,几十秒内「渠道」页显示为正常。

## 调用

```bash
curl -X POST https://solver.example.com/createTask -H "Content-Type: application/json" -d '{
  "clientKey": "sk-…",
  "task": {"type": "TurnstileTaskProxyless", "websiteURL": "https://目标站点/", "websiteKey": "0x4AAAA..."}
}'
# → {"errorId": 0, "taskId": "4817ce94-…"}

curl -X POST https://solver.example.com/getTaskResult -H "Content-Type: application/json" \
  -d '{"clientKey": "sk-…", "taskId": "4817ce94-…"}'
# → {"errorId": 0, "status": "processing"} …… 3 秒后再查 → {"errorId": 0, "status": "ready", "solution": {"token": "…", "userAgent": "…"}}
```

字段与错误码见主 [README](../README.md#接口),控制台「使用文档」页有各语言的示例。

- 所有服务器都满载时 `/solve` 返回 429(带 `Retry-After: 2`),任务接口返回 `ERROR_NO_SLOT_AVAILABLE`,稍后重试即可。
- Cloudflare 的浏览器完整性检查会拦截 Python 标准库 `urllib` 的默认 User-Agent(返回 403,error 1010)。
  用 urllib 调用时请自定义 `User-Agent`;requests、httpx、curl、Go、Node 的默认 UA 不受影响。

## 运维

- **更新 Worker**:有新迁移时先 `npx wrangler@4 d1 migrations apply solver-db --remote --config deploy/wrangler.toml`,
  再 `npx wrangler@4 deploy --config deploy/wrangler.toml`;出问题时 `npx wrangler@4 rollback --config deploy/wrangler.toml`。
- **更新服务器**:镜像由 GitHub Actions(`.github/workflows/image.yml`、`docker-bake.hcl`)在 main 更新时构建;
  在服务器上重新执行安装命令即升级。控制台里修改并发后也要重新执行。
- **日志**:Docker 版 `docker logs turnstile-solver`,`--no-docker` 版 `journalctl -u turnstile-solver`
  和 `/opt/turnstile-solver/state/*.log`。
- **摘流量**:停止接收新任务、等进行中的任务完成后断开隧道:`docker exec turnstile-solver python -m fleet.agent drain`。
  之后 `docker restart turnstile-solver` 恢复。也可以在控制台禁用该渠道(不再分配新任务,已创建任务的结果仍可查询)。
- **数据保留**:Worker 每 5 分钟退还超时未结算的任务,并清理 90 天前已结束的任务记录和 365 天前的使用日志。
- **恢复登录**:开启「仅第三方登录」后若管理员无法登录(例如第三方应用失效),关闭它即可恢复密码登录(设置有 10 秒缓存):

  ```bash
  npx wrangler@4 d1 execute solver-db --remote --config deploy/wrangler.toml --command "DELETE FROM options WHERE key = 'oauth_only_enabled'"
  ```

## 资源

每次求解启动一个全新的浏览器:4 核服务器并发 10 时,40 个任务全部成功、0 次重试,10 个一批 P90 约 25 秒;
4 个同时在解时内存峰值约 2.3GB。安装脚本默认按 CPU 与内存计算并发(最多 32),也可在控制台为每个渠道设置。
