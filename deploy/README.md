# CNB 部署:Turnstile Solver 多 worker + 18 小时无感轮换

## 架构

```
客户端 ──HTTPS──► solver.000.moe ── Cloudflare Worker(cf-worker.js:随机分流 + 失败改投;taskId 固定到一个位置)
                                      │              │              │              │
                                   隧道 A          隧道 B          隧道 C          隧道 D
                                 (本域名源站)   solver-b.000.moe solver-c.000.moe solver-d.000.moe
                                      │              │              │              │
                                  worker [a]     worker [b]     worker [c]     worker [d]    ← CNB 云原生开发环境
                                     每台:cloudflared + 网关(:8686) + FlareSolverr(:8191)   (仅预览模式,4 核,并发 4)

CNB 定时任务(每 10 分钟)── fleet/rotator.py ── CNB OpenAPI:列出 / 创建 / 关闭 worker
```

- **worker**:位置 a 由 `api_trigger_worker` 创建,位置 x(b/c/d)由 `api_trigger_worker_x` 创建。
  镜像基于 `flaresolverr/flaresolverr:v3.5.2`。`launch` 拉起 `fleet/agent.py`,守护 FlareSolverr、网关、cloudflared;
  网关经 FlareSolverr 真实打开一个页面(自检)且隧道连上后,`start-worker` 阶段才算成功。位置 x 使用 `TUNNEL_TOKEN_X`,未配置时暂时接入隧道 A。
  不同构建机的出口 IP 不同(实测 3 台机器 3 个 IP),多台 worker 也能分散单个 IP 的请求量。
- **轮换器**:无状态,每次运行都从 API 读取现状再对齐,每个位置保持 1 个可用 worker,
  替补继承被替换 worker 的位置。某个位置多出来的就绪 worker 会被关掉(先关最早到期的),
  所以手动上线新版本时只需新建 worker,旧的由轮换器回收。
- **分流**:Cloudflare 不会在同一条隧道的多个连接器之间做负载均衡,所以每个位置各用一条隧道,
  由 Worker 随机打乱顺序依次尝试;某一条返回 429(槽位与排队都已满)、503(求解器不可用)、
  无可用连接器(530)或源站连接失败时,改投下一条。每个 worker 并发 4、最多排队 8(`TS_MAX_QUEUE`),
  单次求解总超时上限 85 秒(`TS_MAX_TIMEOUT`),不超过 Cloudflare 100 秒的源站超时。
- **任务**:`createTask` 随机分到某台 worker,返回 `ERROR_NO_SLOT_AVAILABLE` 时 Worker 自动改投下一台;
  taskId 是 UUID 格式,第一位是位置编号(a=0、b=1……),后 7 位是 worker 标识,`getTaskResult` 按第一位路由回原 worker。
  worker 轮换后,它名下尚未取走的结果随之丢失,查询返回 `ERROR_TASKID_INVALID`,重新创建即可(每个位置同一时间只有一台在交接)。
- **交接**:轮换期间,同一位置的新旧 worker 同时连在该位置的隧道上,旧的摘除连接时流量自动切到新的
  (实测关闭旧 worker 期间约 200 个请求无一失败)。

### 生命周期(北京时间)

平台规则:最长 18 小时;运行超过 8 小时且处于 04:00–06:00 时强制回收。
每个 worker 按创建时间推算最早被回收的时刻 `kill`,然后:

| 时刻 | 动作 | 执行方 |
|---|---|---|
| `kill − 80 分钟` 起 | 各位置**依次**拉起替补:每轮最多一个位置,前一个位置的替补还在启动时不开新的 | 轮换器 |
| 替补就绪后的下一轮(≤ 10 分钟) | 关闭该位置的旧 worker;关闭前 `endStages` 让 cloudflared 优雅退出,等待进行中的请求(最多 60 秒) | 轮换器 |
| `kill − 30 分钟` | 兜底:还没轮到的位置不再排队,立即拉起替补 | 轮换器 |
| `kill − 10 分钟` | 兜底:替补始终没起来时,旧 worker 自行摘流量 | worker 自身 |
| `kill − 7 分钟` | 兜底:关闭旧环境 | 轮换器 |

**错开轮换**:同一时间最多只有一个位置在交接,4 个位置约每 10 分钟交接一个,40 分钟左右全部完成,
新 worker 的启动时间也随之错开。替补约 1 分钟就绪(镜像已缓存),每个位置新旧重叠不超过 10 分钟左右;
空缺或启动失败的位置属于补位,不参与排队,立即重建(启动超过 15 分钟未就绪视为失败)。
参数可通过 `FLEET_REPLACE_LEAD_MIN`、`FLEET_DRAIN_MARGIN_MIN` 等环境变量调整,见 `fleet/schedule.py`。

## 一次性配置

### 1. Cloudflare 隧道(每个位置一条)

Cloudflare 控制台 → Zero Trust → 网络 → Tunnels → 创建隧道 → Cloudflared。每条隧道:

1. 在安装命令里复制 `eyJ` 开头的整段 **token**(不用在本机安装)。
2. 添加公共主机名,服务类型选 `HTTP`,URL 填 `localhost:8686`。

| 隧道 | 公共主机名 | 令牌写入 |
|---|---|---|
| A | `solver.000.moe` | `TUNNEL_TOKEN` |
| B | `solver-b.000.moe` | `TUNNEL_TOKEN_B` |
| C | `solver-c.000.moe` | `TUNNEL_TOKEN_C` |
| D | `solver-d.000.moe` | `TUNNEL_TOKEN_D` |

主机名不要用 `b.solver.000.moe` 这种二级子域名:免费证书只覆盖一级子域名。
增减位置时,同步修改 `.cnb.yml`(worker 事件与 `FLEET_SLOTS`、`FLEET_TARGET`)和 `cf-worker.js` 的 `PEER_HOSTS`。

### 2. Cloudflare Worker(分流)

用 Wrangler 部署,代码、路由和开关都在 [`wrangler.toml`](wrangler.toml) 里:

```bash
npx wrangler@4 login                                   # 首次使用时在浏览器里授权
npx wrangler@4 deploy --config deploy/wrangler.toml    # 部署 cf-worker.js 并绑定路由 solver.000.moe/*
npx wrangler@4 rollback --config deploy/wrangler.toml  # 出问题时回退到上一个版本
```

路由只匹配 `solver.000.moe/*`:不要改成 `*.000.moe/*`,那会拦截该域名下的所有站点;也不要用「自定义域」,
那会接管隧道 A 的主机名。`workers.dev` 地址已关闭,Worker 只通过路由提供服务。

当前账号是 Workers 付费版:没有每日请求上限,每月含 1000 万次请求,单次请求的等待时长不限。
如果改用免费版(每天 10 万次),超出后的行为取决于路由的失败模式;界面里没有该开关时,
客户端应在收到 1027 错误时改用 `https://solver-b.000.moe` 等主机名直连其他隧道。

### 3. CNB 访问令牌(给轮换器用)

个人设置 → 访问令牌 → 新建,勾选以下权限:

- `repo-cnb-trigger:rw`:创建 worker
- `repo-cnb-detail:r`:查询状态
- `account-engage:rw`:列出 / 关闭开发环境

### 4. 密钥仓库

密钥仓库 `AzusaMoe/secrets` 不允许令牌访问和 git 推送,只能在网页上编辑。示例见
[`turnstile-worker.example.yml`](turnstile-worker.example.yml) 和 [`turnstile-rotator.example.yml`](turnstile-rotator.example.yml)。

`turnstile-worker.yml`:

```yaml
TUNNEL_TOKEN: "eyJ...(隧道 A 的 token)"
TUNNEL_TOKEN_B: "eyJ...(隧道 B 的 token)"
TUNNEL_TOKEN_C: "eyJ...(隧道 C 的 token)"
TUNNEL_TOKEN_D: "eyJ...(隧道 D 的 token)"
TS_API_KEY: "(自定义的长随机串,客户端调用时使用)"
```

`turnstile-rotator.yml`:

```yaml
CNB_API_TOKEN: "(第 3 步的令牌)"
FLEET_PUBLIC_URL: "https://solver.000.moe"
```

随机串可以这样生成:`python -c "import secrets; print(secrets.token_urlsafe(32))"`

默认只有密钥仓库的负责人 / 管理员触发的流水线才能引用这些文件。定时任务以最后修改 `.cnb.yml` 定时配置的用户身份运行,
worker 则以轮换器令牌的所有者身份创建,所以使用你自己的令牌即可。

### 5. 开启定时任务

仓库 `AzusaMoe/turnstile-solver` → 设置 → 云原生构建 → 打开「**允许定时任务自动触发**」。
开启后 10 分钟内会自动创建 worker。也可以立即手动触发一次轮换:

```bash
curl -X POST -H "Authorization: Bearer $CNB_API_TOKEN" -H "Content-Type: application/json" \
  https://api.cnb.cool/AzusaMoe/turnstile-solver/-/build/start \
  -d '{"branch":"main","event":"api_trigger_rotate"}'
```

## 调用

### 任务接口(YesCaptcha / CapSolver 格式)

```bash
curl -X POST https://solver.000.moe/createTask -H "Content-Type: application/json" -d '{
  "clientKey": "'$TS_API_KEY'",
  "task": {"type": "TurnstileTaskProxyless", "websiteURL": "https://目标站点/", "websiteKey": "0x4AAAA..."}
}'
# → {"errorId": 0, "taskId": "2817ce94-…"}

curl -X POST https://solver.000.moe/getTaskResult -H "Content-Type: application/json" \
  -d '{"clientKey": "'$TS_API_KEY'", "taskId": "2817ce94-…"}'
# → {"errorId": 0, "status": "processing"} …… 3 秒后再查 → {"errorId": 0, "status": "ready", "solution": {"token": "…", "userAgent": "…"}}
```

字段与错误码见主 [README](../README.md#接口)。同步求解:`POST /solve`,`{"url": …, "sitekey": …}` → `{"token": …}`。

`GET /health` 的 `worker` 字段形如 `cnb-xxx/b`,表示请求落在哪个 worker、哪个位置上;
`backend` 为 FlareSolverr 进程是否健康,`active` / `queued` / `rejected` 为并发、排队与被拒数;
`cpu_seconds`、`cpu_limit`、`mem_used_mb` 来自容器 cgroup。

### 注意

- 所有 worker 都满载时 `/solve` 返回 429(带 `Retry-After: 2`),任务接口返回 `ERROR_NO_SLOT_AVAILABLE`,稍后重试即可。
- Cloudflare 的浏览器完整性检查会拦截 Python 标准库 `urllib` 的默认 User-Agent(返回 403,error 1010)。
  用 urllib 调用时请自定义 `User-Agent`;requests、httpx、curl、Go、Node 的默认 UA 不受影响。

## 运维

```bash
export CNB_API_TOKEN=... CNB_REPO=AzusaMoe/turnstile-solver FLEET_SLOTS=a,b,c,d FLEET_TARGET=4 FLEET_VERSION=5
python -m fleet.rotator status               # 各 worker 的位置、状态与替换 / 摘流量 / 关闭时间
python -m fleet.rotator reconcile --dry-run  # 只显示下一轮会执行的操作
```

- **上线新版本**:把 `.cnb.yml` 里轮换器的 `FLEET_VERSION` 加一,和代码一起推送。轮换器会把版本号写进新 worker 的构建标题,
  版本不符的 worker 视同到期,按错开规则逐个替换(每轮一个位置,约 10 分钟一个)。只改文档时不用动版本号。
  想加快进度,可以在每个位置的替补就绪后手动触发一次 `api_trigger_rotate`。
- **账号并发上限**:CNB 每个账号最多同时运行 **6 个** CPU 开发环境(含其他仓库和自检环境),超出时新环境会在 Prepare 阶段被拒绝。
  轮换器会统计账号下全部运行中的环境,新建数量不超过 `FLEET_MAX_WORKSPACES`(默认 6);
  4 个位置平时占 4 个名额,错开交接时最多 5 个。不要一次手动起多台 worker。
- **自检**:触发 `api_trigger_selftest` 事件,会启动一个不接隧道的 worker,验证镜像、浏览器与 Turnstile 链路。它不受轮换器管理,用完要手动关闭。
- **全部停止**:先关闭「允许定时任务自动触发」,再在「头像 → 我的云原生开发」中关闭环境,或调用 `POST /workspace/stop`。
- **日志**:每个 worker 的流水线日志里能看到 launch 与 `start-worker` 阶段的输出;容器内日志位于 `/tmp/turnstile-agent.log` 和 `/tmp/turnstile-agent/*.log`。

## 控制台(用户与积分)

Worker 代码:`cf-worker.js`(分流与计费)、`api.js`(控制台接口)、`db.js`(D1 与积分)、`admin.html`(页面)。
数据库为 D1 `solver-db`,表结构见 `migrations/`;每 5 分钟的定时触发器退还超时未结算的任务,
并清理 90 天前已结束的任务记录和 365 天前的使用日志。

```bash
npx wrangler@4 d1 migrations apply solver-db --remote --config deploy/wrangler.toml   # 新增迁移时
npx wrangler@4 deploy --config deploy/wrangler.toml
```

服务器渠道:控制台「系统设置 → 服务器渠道」填写 Cloudflare API Token(权限:账户 · Cloudflare Tunnel · 编辑;
区域 · DNS · 编辑;区域 · 区域 · 读取,区域选 000.moe)。添加服务器时 Worker 用它建隧道与 DNS 记录;
安装脚本用安装令牌从 `/api/channel/config` 取得位置、隧道令牌与 API Key;`--no-docker` 时不用镜像,
按 `.ide/Dockerfile` 的内容在系统上直接安装(FlareSolverr 版本与补丁写在 `install.sh` 里,升级基础镜像时同步修改)。镜像由 GitHub 公开仓库
TomorrowX6/solver 的 Actions(`.github/workflows/image.yml`、`docker-bake.hcl`)构建。
GitHub 上的历史与 CNB 独立,更新时用 `scripts/publish-github.sh "说明"` 把 main 的当前代码作为一个新提交同步过去。

GitHub 登录:在 GitHub → Settings → Developer settings → OAuth Apps 新建应用,Homepage 填 `https://solver.000.moe`,
回调地址填 `https://solver.000.moe/api/oauth/github/callback`,再把 Client ID / Secret 填到控制台「系统设置」。

LINUX DO 登录:在 connect.linux.do → 应用接入 → 申请接入新建应用,应用主页填 `https://solver.000.moe`,
回调地址填 `https://solver.000.moe/api/oauth/linuxdo/callback`,最低等级在那里设置;再把 Client ID / Secret 填到控制台「系统设置」。

开启「仅第三方登录」后若管理员无法登录(例如第三方应用失效),在本机关闭它即可恢复密码登录:
`npx wrangler@4 d1 execute solver-db --remote --config deploy/wrangler.toml --command "DELETE FROM options WHERE key = 'oauth_only_enabled'"`
(设置有 10 秒缓存)。

初始化前(D1 中没有用户)Worker 只做分流,行为与之前相同。根密钥默认在初始化时校验后存入 D1;
也可以改用 Worker 密钥 `SOLVER_KEY`(`npx wrangler@4 secret put SOLVER_KEY --config deploy/wrangler.toml`),两者都有时以密钥为准。

## 真实站点测试结果(2026-10-05,FLEET_VERSION 8)

用 `testsite/`(`turnstile-test.000.moe`,托管模式组件)做端到端测试:经公网入口 `createTask` + `getTaskResult` 求解,
再把 token 交给站点后端调用 siteverify 校验(同时核对 hostname 与 action):

| 同时发起 | 通过 siteverify | 求解耗时(中位 / P90 / 最慢) |
|---|---|---|
| 8 | 8/8 | 19.3s / 25.7s / 25.7s |
| 16 | 16/16 | 17.1s / 22.1s / 24.1s |
| 24 | 24/24 | 18.7s / 30.1s / 42.0s |

合计 48/48,没有任务需要重试;上一版(v7)为 45/48,失败均为「点击后无 token」。
v8 的两处关键修改:点击改由组件事件驱动(复选框出现后才点,几乎都是点一次就拿到 token);
浏览器以 `about:blank` 启动(默认新标签页请求 Google,在国内网络下挂起,导致约 1/5 的浏览器第一次打开页面就超时)。
单台 worker(并发 5)上 60 个任务 0 次重试,5 个一批的 P90 从 45–78 秒降到 18–21 秒。

作为对比,之前的 Playwright 版 solver 在同一站点上拿到的 token 全部被 siteverify 判为无效(`invalid-input-response`)。
复测:触发 `api_trigger_e2e`(可用 `env` 传 `E2E_ROUNDS`、`E2E_TABS`)。

## 自动扩缩容

轮换器每 5 分钟运行一次,在 `FLEET_MIN`–`FLEET_MAX`(当前 2–4)之间调整 worker 数:

- 负载 = 各 worker 正在求解 + 排队的数量,以及最近每分钟的平均并发(按求解耗时估算);
- 负载超过总容量的 70% 时扩容(一次扩到需要的数量);少一台后 30 分钟内的峰值仍低于剩余容量的 35% 时缩容一台,
  15 分钟内有新 worker 启动时不缩容;
- 所有 worker 都繁忙(返回无空闲名额)时,solver.000.moe 的 Worker 立即触发一次轮换器(需要 Worker 密钥 `CNB_TOKEN`),全局 3 分钟最多一次;
- 新 worker 约 2–3 分钟就绪,突发流量在此期间会收到 `ERROR_NO_SLOT_AVAILABLE`,客户端稍后重试即可;
- 下线(缩容或到期轮换)时 worker 先停止接收新任务,等进行中的任务完成(最多 2 分钟)再断开隧道,已创建的任务都能取到结果。

单台 worker(4 核)并发 10:实测 40 个任务全部成功、0 次重试,10 个一批 P90 约 25 秒,吞吐约 0.4 个/秒。

## 资源与风险

- **核时消耗**:每个 worker 4 核 × 24 小时 × 30 天 ≈ 2,880 核时/月;空闲时保留 2 台(约 5,760 核时/月),满 4 台时约 11,520 核时/月。
  只用免费额度(1,600 核时)大约够 4 天;想省额度可以调小 `.cnb.yml` 中的 `runner.cpus` 和 `TS_MAX_CONCURRENCY`。
- **资源**:FlareSolverr 每个请求启动一个浏览器,单台 worker 4 个同时在解时内存峰值约 2.3GB、CPU 约 10–55%(4 核);
- **平台管控**:CNB 文档写明「为防止滥用,集群可能会对最大时长进行动态管控」。用开发环境长期跑服务不是平台设计的用途,
  回收规则或额度随时可能收紧,也存在账号被限制的风险。
- **隧道协议**:默认使用 `http2`(比 QUIC 更不容易被干扰),可以在 `turnstile-worker.yml` 里加 `TUNNEL_PROTOCOL: quic` 切换。
