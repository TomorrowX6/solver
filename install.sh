#!/usr/bin/env bash
# Turnstile Solver 服务器渠道安装脚本
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/TomorrowX6/solver/main/install.sh) -e <控制台地址> -t <安装令牌>
#   bash <(curl -fsSL https://raw.githubusercontent.com/TomorrowX6/solver/main/install.sh) --uninstall
#
# 选项:
#   -e, --endpoint URL     控制台地址,例如 https://solver.000.moe
#   -t, --token TOKEN      控制台「渠道」页给出的安装令牌
#   --concurrency N        同时求解数;默认用控制台为该渠道设置的值,未设置时按 CPU 与内存计算
#   --no-docker            不用 Docker,直接装在系统上并注册为 systemd 服务(需要 Debian / Ubuntu 系;
#                          浏览器用系统源的 Chromium,没有时 amd64 装 Google Chrome)
#   --image IMAGE          镜像,默认 ghcr.io/tomorrowx6/solver:latest
#   --cn                   国内服务器:用阿里云镜像安装 Docker;--no-docker 时 Python 包用阿里云 PyPI 镜像
#   --uninstall            停止并删除容器或服务、镜像与配置
#
# 重复执行同一条命令即升级。
set -euo pipefail

ENDPOINT="" TOKEN="" CONCURRENCY="" CN=0 UNINSTALL=0 NATIVE=0
IMAGE="ghcr.io/tomorrowx6/solver:latest"
DIR=/opt/turnstile-solver
NAME=turnstile-solver
UNIT=/etc/systemd/system/$NAME.service
SERVICE_USER=turnstile
# 与 .ide/Dockerfile 的基础镜像版本一致(两处同步修改)
FLARESOLVERR_VERSION=v3.5.2
CODE_URL=https://codeload.github.com/TomorrowX6/solver/tar.gz/refs/heads/main

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -e|--endpoint) ENDPOINT="${2:-}"; shift ;;
    -t|--token) TOKEN="${2:-}"; shift ;;
    --concurrency) CONCURRENCY="${2:-}"; shift ;;
    --no-docker) NATIVE=1 ;;
    --image) IMAGE="${2:-}"; shift ;;
    --cn) CN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,18p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "未知参数:$1" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "请用 root 运行"
[ "$(uname -s)" = Linux ] || die "只支持 Linux"
case "$(uname -m)" in x86_64|amd64|aarch64|arm64) ;; *) die "不支持的架构 $(uname -m)" ;; esac

has_docker_install() { command -v docker >/dev/null 2>&1 && docker inspect "$NAME" >/dev/null 2>&1; }
has_native_install() { [ -f "$UNIT" ]; }

remove_docker_install() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}

# 停止并删除服务、服务用户与程序文件;worker.env 保留(切换到 Docker 时仍要用)
remove_native_install() {
  systemctl disable --now "$NAME" >/dev/null 2>&1 || true
  rm -f "$UNIT" /etc/logrotate.d/$NAME
  systemctl daemon-reload >/dev/null 2>&1 || true
  if [ -f "$DIR/held-packages" ]; then
    # shellcheck disable=SC2046
    apt-mark unhold $(cat "$DIR/held-packages") >/dev/null 2>&1 || true
  fi
  userdel "$SERVICE_USER" >/dev/null 2>&1 || true
  (cd "$DIR" 2>/dev/null && rm -rf bin python venv cache flaresolverr src home state chromedriver-linux64     package.json requirements.txt held-packages)
}

if [ "$UNINSTALL" = 1 ]; then
  say "卸载"
  if command -v docker >/dev/null 2>&1; then
    remove_docker_install
    docker rmi "$IMAGE" >/dev/null 2>&1 || true
  fi
  has_native_install && remove_native_install
  rm -rf "$DIR"
  say "已卸载。控制台里的渠道请在「渠道」页删除"
  exit 0
fi

[ -n "$ENDPOINT" ] && [ -n "$TOKEN" ] || die "缺少 -e 或 -t:请复制控制台「渠道」页给出的完整命令"
ENDPOINT="${ENDPOINT%/}"

if [ "$NATIVE" = 1 ]; then
  command -v apt-get >/dev/null 2>&1 || die "--no-docker 只支持 Debian / Ubuntu 系(apt);其他系统请去掉 --no-docker 用 Docker 安装"
  [ -d /run/systemd/system ] || die "--no-docker 需要 systemd;请去掉 --no-docker 用 Docker 安装"
fi

say "下载配置"
mkdir -p "$DIR"
curl -fsS -H "Authorization: Bearer $TOKEN" "$ENDPOINT/api/channel/config" -o "$DIR/worker.env.tmp" \
  || die "下载配置失败:地址或安装令牌错误,或该渠道已在控制台删除"

# 并发:命令行 --concurrency > 控制台设置 > 自动(每个浏览器约 0.45GB 内存,另留 1.5GB;4 核 8GB 约 10 个)
configured=$(sed -n 's/^TS_MAX_CONCURRENCY=//p' "$DIR/worker.env.tmp")
sed -i '/^TS_MAX_CONCURRENCY=/d' "$DIR/worker.env.tmp"
[ -n "$CONCURRENCY" ] || CONCURRENCY="$configured"
if [ -z "$CONCURRENCY" ]; then
  cores=$(nproc)
  mem_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
  by_cpu=$(( cores * 5 / 2 ))
  by_mem=$(( (mem_mb - 1536) / 460 ))
  CONCURRENCY=$(( by_cpu < by_mem ? by_cpu : by_mem ))
  [ "$CONCURRENCY" -le 32 ] || CONCURRENCY=32  # 自动计算的上限;需要更多时用 --concurrency 或在控制台指定
fi
[ "$CONCURRENCY" -ge 1 ] 2>/dev/null || CONCURRENCY=1
{
  cat "$DIR/worker.env.tmp"
  echo "TS_MAX_CONCURRENCY=$CONCURRENCY"
  echo "TS_MAX_QUEUE=$CONCURRENCY"
  echo "WAITRESS_THREADS=$(( CONCURRENCY + 4 ))"
} > "$DIR/worker.env"
rm -f "$DIR/worker.env.tmp"
chmod 600 "$DIR/worker.env"

# ---------------------------------------------------------------- Docker
install_docker() {
  # 之前用 --no-docker 装过时先停掉,避免两份进程连同一条隧道
  if has_native_install; then
    say "删除非 Docker 版服务"
    remove_native_install
  fi

  if ! command -v docker >/dev/null 2>&1; then
    say "安装 Docker"
    if [ "$CN" = 1 ]; then
      curl -fsSL https://get.docker.com | sh -s -- --mirror Aliyun
    else
      curl -fsSL https://get.docker.com | sh
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || die "Docker 未运行"

  say "拉取镜像 $IMAGE"
  docker pull "$IMAGE" >/dev/null

  say "启动(并发 $CONCURRENCY)"
  remove_docker_install
  docker run -d --name "$NAME" --restart unless-stopped --shm-size 2g --env-file "$DIR/worker.env" "$IMAGE" >/dev/null

  say "等待自检(最多 5 分钟)"
  if docker exec -e AGENT_WAIT_TIMEOUT=300 "$NAME" python -m fleet.agent wait >/dev/null 2>"$DIR/wait.log"; then
    return 0
  fi
  tail -n 40 "$DIR/wait.log" >&2 || true
  die "自检未通过。日志:docker exec $NAME tail -n 50 /tmp/turnstile-agent/flaresolverr.log"
}

# ---------------------------------------------------------------- 非 Docker
# 包在当前源中有可安装的版本(Ubuntu 上 chromium 只是个没有候选版本的名字)。
# 先取完输出再匹配:grep -q 提前退出会让 apt-cache 收到 SIGPIPE,在 pipefail 下判为失败
has_candidate() {
  local policy
  policy=$(LC_ALL=C apt-cache policy "$1" 2>/dev/null)
  [[ $policy =~ Candidate:\ [^\(] ]]
}

install_browser() {
  local pkgs=""
  if [ -f "$DIR/held-packages" ]; then
    pkgs=$(cat "$DIR/held-packages")
    # shellcheck disable=SC2086
    apt-mark unhold $pkgs >/dev/null 2>&1 || true
  fi

  if has_candidate chromium && has_candidate chromium-common && has_candidate chromium-driver; then
    say "安装 Chromium(系统源)"
    apt-get install -y -qq --no-install-recommends chromium chromium-common chromium-driver >/dev/null
    pkgs="chromium chromium-common chromium-driver"
    BROWSER=/usr/bin/chromium
    DRIVER_SRC=$(readlink -f /usr/bin/chromedriver)
  elif [ "$(dpkg --print-architecture)" = amd64 ]; then
    # Ubuntu 的 chromium 只有 snap 版,改用 Google Chrome + 同版本的 chromedriver(Chrome for Testing)
    say "安装 Google Chrome"
    curl -fsSL --retry 3 -o /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y -qq /tmp/google-chrome.deb >/dev/null
    rm -f /tmp/google-chrome.deb
    pkgs="google-chrome-stable"
    BROWSER=/usr/bin/google-chrome-stable
    local version build url
    version=$(google-chrome-stable --version | grep -oE '[0-9]+(\.[0-9]+){3}')
    url="https://storage.googleapis.com/chrome-for-testing-public/$version/linux64/chromedriver-linux64.zip"
    if ! curl -fsSI "$url" >/dev/null 2>&1; then
      # 该补丁版本没有 chromedriver 时取同一 build 的最新版
      build=${version%.*}
      version=$(curl -fsSL https://googlechromelabs.github.io/chrome-for-testing/latest-patch-versions-per-build.json \
        | sed -n "s/.*\"${build//./\\.}\":{\"version\":\"\([0-9.]*\)\".*/\1/p")
      [ -n "$version" ] || die "找不到 Chrome $build 对应的 chromedriver"
      url="https://storage.googleapis.com/chrome-for-testing-public/$version/linux64/chromedriver-linux64.zip"
    fi
    curl -fsSL --retry 3 -o /tmp/chromedriver.zip "$url"
    rm -rf "$DIR/chromedriver-linux64"
    unzip -q -o /tmp/chromedriver.zip -d "$DIR"
    rm -f /tmp/chromedriver.zip
    DRIVER_SRC="$DIR/chromedriver-linux64/chromedriver"
  else
    die "系统源没有 Chromium,Google Chrome 也只有 amd64 版;请去掉 --no-docker 用 Docker 安装"
  fi
  # 锁定版本:浏览器被自动更新后与 chromedriver 不匹配会导致求解失败。重新执行安装命令时解锁并升级
  # shellcheck disable=SC2086
  apt-mark hold $pkgs >/dev/null
  echo "$pkgs" > "$DIR/held-packages"
}

# 目录:bin(uv、cloudflared)、python(uv 管理的 Python)、venv、flaresolverr(源码,打补丁)、
# src(本仓库的 app/、fleet/、run.py)、home(服务用户的主目录与 chromedriver)、state(进程日志与状态)
install_native() {
  if has_docker_install; then
    say "删除 Docker 版容器"
    remove_docker_install
  fi
  # 升级时先停服务再替换文件;旧的 state.json 会让自检误判为就绪
  systemctl stop "$NAME" >/dev/null 2>&1 || true
  rm -f "$DIR/state/state.json"

  say "安装系统依赖"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends ca-certificates curl tar unzip procps xvfb xauth >/dev/null
  install_browser

  local arch tmp
  arch=$(uname -m)
  case "$arch" in amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; esac
  mkdir -p "$DIR/bin"

  say "下载 cloudflared"
  curl -fsSL --retry 3 -o "$DIR/bin/cloudflared" \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture)"
  chmod +x "$DIR/bin/cloudflared"
  "$DIR/bin/cloudflared" --version >/dev/null

  say "下载 FlareSolverr $FLARESOLVERR_VERSION 与本项目代码"
  tmp=$(mktemp -d)
  curl -fsSL --retry 3 "https://codeload.github.com/FlareSolverr/FlareSolverr/tar.gz/refs/tags/$FLARESOLVERR_VERSION" | tar -xz -C "$tmp"
  curl -fsSL --retry 3 "$CODE_URL" | tar -xz -C "$tmp"
  rm -rf "$DIR/flaresolverr" "$DIR/src"
  mv "$tmp"/FlareSolverr-*/src "$DIR/flaresolverr"
  # FlareSolverr 从源码目录的上一级读取 package.json(版本号)
  mv "$tmp"/FlareSolverr-*/package.json "$tmp"/FlareSolverr-*/requirements.txt "$DIR/"
  mkdir -p "$DIR/src"
  mv "$tmp"/solver-*/app "$tmp"/solver-*/fleet "$tmp"/solver-*/run.py "$DIR/src/"
  rm -rf "$tmp"

  # 与 .ide/Dockerfile 相同的两处补丁:waitress 线程数由 WAITRESS_THREADS 控制;浏览器以空白页启动
  local fs="$DIR/flaresolverr"
  sed -i 's/serve(handler, host=self.host, port=self.port, asyncore_use_poll=True)/serve(handler, host=self.host, port=self.port, asyncore_use_poll=True, threads=int(os.environ.get("WAITRESS_THREADS", "4")))/' "$fs/flaresolverr.py"
  grep -q 'WAITRESS_THREADS' "$fs/flaresolverr.py" || die "FlareSolverr 补丁失败(flaresolverr.py)"
  sed -i 's/^    options = uc.ChromeOptions()$/    options = uc.ChromeOptions()\n    if "about:blank" not in options.arguments:\n        options.add_argument("about:blank")/' "$fs/utils.py"
  grep -q 'options.add_argument("about:blank")' "$fs/utils.py" || die "FlareSolverr 补丁失败(utils.py)"
  # 镜像里 chromedriver 固定在 /app/chromedriver;这里改为由 CHROMEDRIVER_PATH 指定,不在运行时下载。
  # 浏览器也由 CHROME_PATH 指定:系统里同时有 Chrome 和 Chromium 时 uc 会任选一个,与驱动版本对不上
  sed -i 's#"/app/chromedriver"#os.environ.get("CHROMEDRIVER_PATH", "/app/chromedriver")#g' "$fs/utils.py"
  [ "$(grep -c 'CHROMEDRIVER_PATH' "$fs/utils.py")" = 2 ] || die "FlareSolverr 补丁失败(chromedriver 路径)"
  sed -i 's/^CHROME_EXE_PATH = None$/CHROME_EXE_PATH = os.environ.get("CHROME_PATH") or None/' "$fs/utils.py"
  grep -q 'os.environ.get("CHROME_PATH")' "$fs/utils.py" || die "FlareSolverr 补丁失败(浏览器路径)"

  say "安装 Python 3.11 与依赖"
  curl -fsSL --retry 3 "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-unknown-linux-gnu.tar.gz" \
    | tar -xz -C "$DIR/bin" --strip-components=1
  export UV_PYTHON_INSTALL_DIR="$DIR/python" UV_PYTHON_PREFERENCE=only-managed UV_CACHE_DIR="$DIR/cache"
  if [ "$CN" = 1 ]; then export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/; fi
  rm -rf "$DIR/venv"
  "$DIR/bin/uv" venv -q --python 3.11 "$DIR/venv"
  # 网关依赖与 .ide/Dockerfile 一致
  "$DIR/bin/uv" pip install -q --python "$DIR/venv/bin/python" -r "$DIR/requirements.txt" \
    "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27" "pydantic>=2.6"

  # 以独立用户运行;uc 会改写 chromedriver,所以给服务用户一份自己的副本
  id -u "$SERVICE_USER" >/dev/null 2>&1 \
    || useradd --system --home-dir "$DIR/home" --shell /usr/sbin/nologin "$SERVICE_USER"
  mkdir -p "$DIR/home" "$DIR/state"
  install -m 755 "$DRIVER_SRC" "$DIR/home/chromedriver"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$DIR/home" "$DIR/state"

  cat > "$UNIT" <<EOF
[Unit]
Description=Turnstile Solver worker
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DIR/src
EnvironmentFile=$DIR/worker.env
Environment=PATH=$DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=AGENT_STATE_DIR=$DIR/state FLARESOLVERR_DIR=$DIR/flaresolverr TS_FLARESOLVERR_DIR=$DIR/flaresolverr
Environment=CHROME_PATH=$BROWSER CHROMEDRIVER_PATH=$DIR/home/chromedriver
Environment=AGENT_PERMANENT=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8
ExecStart=$DIR/venv/bin/python -m fleet.agent run
Restart=always
RestartSec=10
# 只给守护进程发 SIGTERM,由它先摘隧道再停子进程;超时后整组强杀(含残留的 Chrome、Xvfb)
KillMode=mixed
TimeoutStopSec=120
PrivateTmp=yes
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  # 子进程日志以追加方式打开,可用 copytruncate 轮转
  if [ -d /etc/logrotate.d ]; then
    cat > /etc/logrotate.d/$NAME <<EOF
$DIR/state/*.log {
  daily
  rotate 3
  maxsize 50M
  compress
  missingok
  notifempty
  copytruncate
}
EOF
  fi

  say "启动(并发 $CONCURRENCY)"
  systemctl daemon-reload
  systemctl enable "$NAME" >/dev/null 2>&1
  systemctl restart "$NAME"

  say "等待自检(最多 5 分钟)"
  if (cd "$DIR/src" && AGENT_STATE_DIR="$DIR/state" AGENT_WAIT_TIMEOUT=300 "$DIR/venv/bin/python" -m fleet.agent wait) \
    >/dev/null 2>"$DIR/wait.log"; then
    return 0
  fi
  tail -n 40 "$DIR/wait.log" >&2 || true
  die "自检未通过。日志:journalctl -u $NAME -n 50;tail -n 50 $DIR/state/flaresolverr.log"
}

if [ "$NATIVE" = 1 ]; then
  install_native
else
  install_docker
fi
slot=$(sed -n 's/^FLEET_SLOT=//p' "$DIR/worker.env")
say "完成:渠道 $slot 已上线,可在控制台「渠道」页查看"
