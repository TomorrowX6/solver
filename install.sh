#!/usr/bin/env bash
# Turnstile Solver 服务器渠道安装脚本
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/TomorrowX6/solver/main/install.sh) -e <控制台地址> -t <安装令牌>
#   bash <(curl -fsSL https://raw.githubusercontent.com/TomorrowX6/solver/main/install.sh) --uninstall
#
# 选项:
#   -e, --endpoint URL     控制台地址,例如 https://solver.000.moe
#   -t, --token TOKEN      控制台「渠道」页给出的安装令牌
#   --concurrency N        同时求解数,默认按 CPU 与内存计算
#   --image IMAGE          镜像,默认 ghcr.io/tomorrowx6/solver:latest
#   --cn                   用阿里云镜像安装 Docker(国内服务器)
#   --uninstall            停止并删除容器、镜像与配置
#
# 重复执行同一条命令即升级到最新镜像。
set -euo pipefail

ENDPOINT="" TOKEN="" CONCURRENCY="" CN=0 UNINSTALL=0
IMAGE="ghcr.io/tomorrowx6/solver:latest"
DIR=/opt/turnstile-solver
NAME=turnstile-solver

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    -e|--endpoint) ENDPOINT="${2:-}"; shift ;;
    -t|--token) TOKEN="${2:-}"; shift ;;
    --concurrency) CONCURRENCY="${2:-}"; shift ;;
    --image) IMAGE="${2:-}"; shift ;;
    --cn) CN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,16p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "未知参数:$1" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "请用 root 运行"
[ "$(uname -s)" = Linux ] || die "只支持 Linux"
case "$(uname -m)" in x86_64|amd64|aarch64|arm64) ;; *) die "不支持的架构 $(uname -m)" ;; esac

if [ "$UNINSTALL" = 1 ]; then
  say "卸载"
  if command -v docker >/dev/null 2>&1; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rmi "$IMAGE" >/dev/null 2>&1 || true
  fi
  rm -rf "$DIR"
  say "已卸载。控制台里的渠道请在「渠道」页删除"
  exit 0
fi

[ -n "$ENDPOINT" ] && [ -n "$TOKEN" ] || die "缺少 -e 或 -t:请复制控制台「渠道」页给出的完整命令"
ENDPOINT="${ENDPOINT%/}"

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

say "下载配置"
mkdir -p "$DIR"
curl -fsS -H "Authorization: Bearer $TOKEN" "$ENDPOINT/api/channel/config" -o "$DIR/worker.env.tmp" \
  || die "下载配置失败:地址或安装令牌错误,或该渠道已在控制台删除"

# 并发:每个浏览器约 0.45GB 内存,另留 1.5GB;4 核 8GB 约 10 个
if [ -z "$CONCURRENCY" ]; then
  cores=$(nproc)
  mem_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
  by_cpu=$(( cores * 5 / 2 ))
  by_mem=$(( (mem_mb - 1536) / 460 ))
  CONCURRENCY=$(( by_cpu < by_mem ? by_cpu : by_mem ))
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

say "拉取镜像 $IMAGE"
docker pull "$IMAGE" >/dev/null

say "启动(并发 $CONCURRENCY)"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped --shm-size 2g --env-file "$DIR/worker.env" "$IMAGE" >/dev/null

say "等待自检(最多 5 分钟)"
if docker exec -e AGENT_WAIT_TIMEOUT=300 "$NAME" python -m fleet.agent wait >/dev/null 2>"$DIR/wait.log"; then
  slot=$(sed -n 's/^FLEET_SLOT=//p' "$DIR/worker.env")
  say "完成:渠道 $slot 已上线,可在控制台「渠道」页查看"
else
  tail -n 40 "$DIR/wait.log" >&2 || true
  die "自检未通过。日志:docker exec $NAME tail -n 50 /tmp/turnstile-agent/flaresolverr.log"
fi
