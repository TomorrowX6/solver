# Turnstile Solver

传入网址和 sitekey,返回 Cloudflare Turnstile token。接口与 YesCaptcha / CapSolver 一致(`createTask` + `getTaskResult`),
现有打码平台客户端只需改服务地址和 `clientKey`。

求解使用 [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)(v3.5.2)的反检测浏览器(undetected-chromedriver):
在目标域名下打开一个页面(先试 `/robots.txt`,不行再用传入的网址),换成用指定 sitekey 渲染的组件,点击复选框后取回 token。
页面属于目标域名,所以能通过 Turnstile 的域名校验;实测拿到的 token 能通过站点后端的 siteverify。

另外提供 `POST /solve` 同步求解。

## 启动

在 FlareSolverr 镜像(`flaresolverr/flaresolverr:v3.5.2`)里运行网关,它会导入 `/app` 下的 FlareSolverr 代码来启动浏览器:

```bash
pip install -r requirements.txt
python run.py --api-key mykey --backend-url http://127.0.0.1:8191   # 网关:http://127.0.0.1:8000,文档见 /docs
```

部署到 CNB 的完整方式(镜像、守护进程、隧道)见 [deploy/README.md](deploy/README.md)。

## 接口

### 创建任务 `POST /createTask`

```json
{
  "clientKey": "你的 API Key",
  "task": {
    "type": "TurnstileTaskProxyless",
    "websiteURL": "https://react-turnstile.vercel.app",
    "websiteKey": "1x00000000000000000000AA"
  }
}
```

```json
{"errorId": 0, "errorCode": "", "errorDescription": "", "taskId": "1817ce94-3f2a-4b1c-9d0e-5a6b7c8d9e0f"}
```

- `type`:`TurnstileTaskProxyless`(也接受 `AntiTurnstileTaskProxyLess` 等写法,不区分大小写)。
  不支持经调用方的代理求解:带代理的 `TurnstileTask` 返回 `ERROR_TASK_NOT_SUPPORTED`。
- 可选:`metadata.action`、`metadata.cdata`(也接受顶层 `action` / `pageAction`、`cdata` / `data`),需与站点渲染组件时的参数一致。

### 获取结果 `POST /getTaskResult`

```json
{"clientKey": "你的 API Key", "taskId": "1817ce94-3f2a-4b1c-9d0e-5a6b7c8d9e0f"}
```

| 情况 | 响应 |
|---|---|
| 识别中 | `{"errorId": 0, "status": "processing"}`,3 秒后再查 |
| 完成 | `{"errorId": 0, "status": "ready", "solution": {"token": "…", "userAgent": "…"}}` |
| 出错 | `{"errorId": 1, "errorCode": "…", "errorDescription": "…"}` |

token 一次性使用,建议拿到后 60 秒内提交;结果在服务端保留 300 秒。

### 余额 `POST /getBalance`

`{"clientKey": "…"}` → `{"errorId": 0, "balance": 999999}`。自建服务不计费,只为兼容会先查余额的客户端。

### 错误码

打码平台风格的接口一律返回 HTTP 200,用 `errorId` / `errorCode` 表示结果:

| errorCode | 含义 |
|---|---|
| `ERROR_KEY_DOES_NOT_EXIST` | clientKey 错误 |
| `ERROR_TASK_NOT_SUPPORTED` | 不支持的任务类型(包括带代理的 `TurnstileTask`) |
| `ERROR_INVALID_TASK_DATA` | 缺少 task、websiteURL / websiteKey,或 websiteURL 不是 http(s) 地址 |
| `ERROR_NO_SLOT_AVAILABLE` | 当前没有空闲名额,稍后重试 |
| `ERROR_TASKID_INVALID` | taskId 不存在、已过期,或创建它的 worker 已轮换下线(重新创建即可) |
| `ERROR_CAPTCHA_UNSOLVABLE` | 识别失败:sitekey 与域名不匹配、超时等,详见 errorDescription |
| `ERROR_SERVICE_UNAVALIABLE` | 求解器暂不可用 |
| `ERROR_ZERO_BALANCE` | 积分或令牌额度不足(使用用户令牌时) |

### 同步求解 `POST /solve`

```bash
curl -X POST http://127.0.0.1:8000/solve -H "X-API-Key: mykey" -H "Content-Type: application/json" \
  -d '{"url": "https://目标站点/", "sitekey": "0x4AAAA...", "action": "login", "timeout": 60}'
# → {"token": "…", "elapsed": 9.7, "attempts": 1, "user_agent": "…"}
```

出错时为 `{"status": "error", "code": "…", "message": "…"}`:401 `unauthorized`、422 `turnstile_error`(sitekey 与域名不匹配等)、
429 `busy`、500 `timeout` / `page_error`、503 `solver_unavailable`。参数校验失败(包括传入不再支持的 `proxy`)时为 422。

### 控制台与积分

`https://solver.000.moe`(由 Cloudflare Worker 提供,数据存于 D1,参照 NewAPI):用户、令牌(`sk-…`)、积分、兑换码、使用日志、渠道状态、系统设置。

- 首次打开时初始化超级管理员,需填写现有的 API Key(校验后作为转发 worker 的根密钥保存)。
- 调用方文档在控制台「使用文档」页,未登录也可访问:`https://solver.000.moe/#docs`。
- 用户令牌即 `clientKey` / `X-API-Key`:创建任务时预扣积分,求解成功才结算,失败或 10 分钟未取结果自动退还;`getBalance` 返回账户余额。
- 现有的 API Key 照常可用且不计费。
- 充值方式为管理员生成的兑换码;价格在「系统设置 → 基础设置」,注册开关(密码、GitHub、LINUX DO 分别开关,例如只开放 LINUX DO 注册)、
  新用户赠送、每日签到在「系统设置 → 注册登录」中修改。
- 每日签到:开启后用户每天(北京时间)第一次登录或打开控制台时自动获得设定的积分,同一天只发放一次,记入使用日志。
- 支持 GitHub 与 LINUX DO 登录:在「系统设置 → 第三方登录」填写对应应用的 Client ID / Secret;开放该方式注册时首次登录自动创建账号,
  已有账号可在「个人设置」中绑定。可开启「仅第三方登录」:登录页只显示 GitHub / LINUX DO 按钮,密码登录与密码注册关闭
  (两种都未启用时自动退回密码登录;保存会导致自己无法登录的设置时会被拒绝)。
- worker 的 `GET /admin/stats`(需根密钥)提供渠道页的数据,只保存在进程内,worker 轮换后清零。

### 自有服务器渠道

除了 CNB 上自动扩缩的 worker,任意 Linux 服务器(x86_64 / arm64,建议 4 核 8GB 以上)都可以作为渠道接入,
与 CNB worker 一起分流和计费。控制台「渠道」页点「添加服务器」(需先在「系统设置 → 服务器渠道」填写 Cloudflare API Token),
会自动创建 Cloudflare 隧道和 `solver-<位置>.000.moe` 域名,并给出安装命令,在服务器上以 root 执行:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TomorrowX6/solver/main/install.sh) -e https://solver.000.moe -t <安装令牌>
```

脚本会按需安装 Docker,拉取镜像 `ghcr.io/tomorrowx6/solver`(GitHub Actions 构建,amd64 / arm64),
按控制台为该渠道设置的并发数(未设置时按 CPU 与内存自动计算,最多 32;命令行 `--concurrency N` 优先)
启动容器(开机自启),并等待自检通过。在控制台修改并发后,重新执行安装命令生效。重复执行即升级;`--uninstall` 卸载;
国内服务器加 `--cn` 用阿里云镜像安装 Docker。服务器常驻运行,不参与 CNB 的轮换与扩缩;
在控制台禁用渠道后不再分配新任务,删除渠道会同时删除它的隧道和域名。

不想用 Docker 时在命令末尾加 `--no-docker`(安装窗口里可切换):直接在系统上安装浏览器、Python 3.11(uv 管理)、
FlareSolverr v3.5.2(打与镜像相同的补丁)和 cloudflared,装到 `/opt/turnstile-solver`,以 `turnstile` 用户运行 systemd 服务
`turnstile-solver`。需要 Debian / Ubuntu 系(apt + systemd):系统源有 Chromium 时用它(Debian,amd64 / arm64),
没有时装 Google Chrome 和同版本的 chromedriver(Ubuntu,只支持 amd64)。浏览器包用 `apt-mark hold` 锁定版本,
避免自动更新后与 chromedriver 不匹配,重新执行安装命令即解锁并升级。日志:`journalctl -u turnstile-solver`、
`/opt/turnstile-solver/state/*.log`(logrotate 每天轮转)。同一台服务器换用另一种方式安装时,脚本会先删掉原来的容器或服务。

### `GET /health`

`status`、`backend`(FlareSolverr)、`solver`(sitekey 求解器,不可用时 `solver_error` 给出原因,初始化失败会在后台每 15 秒重试)、`active` / `queued` / `rejected`、`tasks_pending`,
`attempt_errors`(每次尝试失败的原因计数,含随后重试成功的)、`token_stats`,以及容器 cgroup 的 `cpu_seconds`、`cpu_limit`、`mem_used_mb`。

## 求解策略

- 每次求解启动一个全新的浏览器;单次尝试超过 `TS_ATTEMPT_TIMEOUT`(默认 35 秒)或判定卡住时,换新浏览器重试,
  直到总超时(任务为 `TS_MAX_TIMEOUT`,`/solve` 可用 `timeout` 指定)。
  sitekey / 域名配置错误不重试;页面无法加载组件(网络超时或 CSP 拦截)最多重试一次。
- 点击由组件 iframe 发给页面的 postMessage 事件驱动:收到 `interactiveBegin`(复选框出现)后停顿 1~2 秒用鼠标点击;
  点击后 6 秒左右仍未进入验证(没有 `interactiveEnd`)再点,同一个复选框最多点 3 次。复选框出现前点击无效,所以不会提前点。
- 挑战在复选框出现前 `TS_STALL_SECONDS`(默认 20)秒没有新事件时放弃这个浏览器,换新浏览器重试;组件报错(如 600010)时自动 `turnstile.reset()`。
  并发高时挑战计算变慢,阈值过短会误判并引发连锁重试(4 核、并发 10:8 秒时 40 个任务重试 88 次,20 秒时 0 次)。
- token 先从回调取,其次 `turnstile.getResponse()`,最后是组件内的隐藏输入框。
- 浏览器以 `about:blank` 启动。默认的新标签页会请求 Google(国内网络下连接挂起),chromedriver 要等它结束才开始导航,
  约 1/5 的浏览器因此第一次打开页面就超时;改为空白页后同一节点 20 个任务 0 次超时、0 次重试(原来 7 次超时、9 次重试)。
  导航仍然超时时先重试同一个页面,再换后备页面。
- `api.js` 加载失败时退避重试最多 4 次;6 秒既没成功也没报错(连接卡住)时换一个等价地址再请求;
  在 `/robots.txt` 上 12 秒内加载不出组件就改用原网址。
- 每次尝试失败的原因按类别计入 `/health` 的 `attempt_errors`(api.js 未加载、组件卡住、点击后无响应、页面导航超时等);
  `token_stats` 按拿到 token 时的点击次数与来源计数。
- 不要关闭浏览器的 QUIC(HTTP/3):同一节点上的对比实验中,关闭 QUIC 后组件几乎全部卡住(0/4 × 6 批),开启时 4/4 × 6 批。
- `/solve` 与任务共用浏览器名额(`TS_MAX_CONCURRENCY` + 排队 `TS_MAX_QUEUE`),超出返回忙。

## 配置(环境变量)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TS_HOST` / `TS_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `TS_API_KEY` | 空 | API Key(`clientKey` / `X-API-Key`);对外暴露时必须设置 |
| `TS_MAX_CONCURRENCY` | `4` | 同时运行的求解浏览器数 |
| `TS_MAX_QUEUE` | `0` | 名额全满时允许排队的请求数,超出即返回忙;`0` 表示不限 |
| `TS_MAX_TIMEOUT` | `85` | 单次求解的总超时上限(秒) |
| `TS_DEFAULT_TIMEOUT` | `60` | `/solve` 未指定 `timeout` 时的总超时(秒) |
| `TS_ATTEMPT_TIMEOUT` | `35` | 单次尝试的超时(秒),超出换新浏览器重试 |
| `TS_SOLVE_PAGE` | `auto` | 承载组件的页面:`auto`(先 `/robots.txt` 再原网址)、`light`、`full` |
| `TS_SOLVE_INJECT` | `write` | 组件注入方式:`write`(document.write)或 `innerhtml` |
| `TS_CLICK_MODE` | `mouse` | 点击方式:`mouse`、`keyboard`(Tab + 空格,实测基本无效)、`alternate`(交替) |
| `TS_CHROME_ARGS` | 空 | 追加给求解浏览器的启动参数(空格分隔) |
| `TS_CHROME_ARGS_FILE` | 空 | 参数文件(每行一个参数),每次启动浏览器都重新读取,便于在同一环境里对比不同参数 |
| `TS_DEBUG_DIR` | 空 | 设置后把组件截图(点击前、点击后、放弃时)保存到该目录 |
| `TS_STALL_SECONDS` | `20` | 复选框出现前多少秒没有新事件判定为卡住 |
| `TS_DRAIN_FILE` | 空 | 下线标记文件:存在时不接受新任务(返回繁忙,上游改投),只返回已有任务的结果 |
| `TS_TASK_PREFIX` | 空 | taskId 第一段的前缀(多副本部署时用于路由) |
| `TS_TASK_TTL` / `TS_MAX_PENDING_TASKS` | `300` / `200` | 任务结果保留秒数 / 最多未完成任务数 |
| `TS_BACKEND_URL` | `http://127.0.0.1:8191` | FlareSolverr 地址(只用于 `/health` 的健康检查) |
| `TS_FLARESOLVERR_DIR` | `/app` | FlareSolverr 源码目录(求解器从这里导入浏览器工具) |
| `TS_WORKER_ID` | 空 | 在 `/health` 中返回,用于区分副本 |

## 测试

```bash
python -m pytest                                          # 离线测试(模拟的求解器与 FlareSolverr)
SOLVER_URL=… TS_API_KEY=… python examples/e2e_real_site.py   # 真实 Turnstile 站点端到端,token 交给 siteverify 校验(见 testsite/)
```

`E2E_MODE` 可选 `task`(默认,createTask + getTaskResult)或 `solve`。

## 说明

- 并发与任务状态都在进程内,网关只能**单进程**运行(不要使用 uvicorn `--workers`)。
- 仅限在你拥有或获得授权的站点上使用,并遵守目标站点的服务条款。
