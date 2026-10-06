#!/bin/bash
# start-worker 阶段:守护进程已由 launch.sh 拉起,这里等待其通过自检并连上隧道。
# 阶段成功即代表 worker 可用,轮换器据此判断。
set -euo pipefail
cd "${CNB_BUILD_WORKSPACE:-/workspace}"
python -m fleet.agent wait
