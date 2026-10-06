#!/usr/bin/env bash
# 把 main 的当前代码作为一个新提交同步到 GitHub 公开仓库 TomorrowX6/solver。
# GitHub 上的历史与 CNB 独立(不含 CNB 上的旧提交),所以不能直接 git push main。
#   scripts/publish-github.sh "提交说明"
set -euo pipefail
REPO=https://github.com/TomorrowX6/solver.git
msg="${1:?用法:scripts/publish-github.sh \"提交说明\"}"

git fetch -q "$REPO" "+main:refs/heads/github"
parent=$(git rev-parse github)
tree=$(git rev-parse 'main^{tree}')
if [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
  echo "GitHub 已是最新"
  exit 0
fi
commit=$(git commit-tree -S "$tree" -p "$parent" -m "$msg")
git branch -f github "$commit"
git -c credential.helper= -c credential.helper='!gh auth git-credential' push -q "$REPO" github:refs/heads/main
echo "已同步:$(git rev-parse --short "$commit")"
