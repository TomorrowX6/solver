#!/bin/bash
# 仅预览模式的 launch 命令(在 Prepare 阶段、stages 之前执行):
# 后台拉起守护进程,确认业务监听 8686 后退出。完整就绪由 start-worker 阶段判断。
cd "${CNB_BUILD_WORKSPACE:-/workspace}"

if ! (echo > /dev/tcp/127.0.0.1/8686) 2>/dev/null; then
  setsid nohup python -m fleet.agent run >> /tmp/turnstile-agent.log 2>&1 < /dev/null &
fi

for _ in $(seq 1 120); do
  if (echo > /dev/tcp/127.0.0.1/8686) 2>/dev/null; then
    echo "gateway is listening on 8686"
    exit 0
  fi
  sleep 2
done
echo "gateway is not listening on 8686" >&2
tail -n 50 /tmp/turnstile-agent.log /tmp/turnstile-agent/gateway.log /tmp/turnstile-agent/flaresolverr.log >&2 || true
exit 1
