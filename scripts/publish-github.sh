#!/usr/bin/env bash
# 同步到 GitHub 公开仓库 TomorrowX6/solver:
#   cnb  分支 ← 本仓库 main 的完整代码(含 CNB 部署)
#   main 分支 ← 本仓库 public 分支:不含 CNB 部署的版本。先把 main 合并进 public 再推
# GitHub 上的历史与 CNB 独立(不含 CNB 上的旧提交),每次把目录树作为一个新提交推上去。
# 推送后记得把 public 分支也推到 CNB,下次在它的基础上合并。
#   scripts/publish-github.sh "提交说明"
set -euo pipefail
REPO=https://github.com/TomorrowX6/solver.git
msg="${1:?用法:scripts/publish-github.sh \"提交说明\"}"
# public 分支里删掉的 CNB 专用文件:合并时 main 对它们的修改直接丢弃
CNB_ONLY=(.cnb.yml .ide/launch.sh .ide/start-worker.sh fleet/cnb.py fleet/rotator.py fleet/schedule.py
  deploy/turnstile-rotator.example.yml deploy/turnstile-worker.example.yml scripts/publish-github.sh tests/test_fleet.py)
# public 分支有自己的版本(自托管部署文档):冲突时保留 public 的
PUBLIC_OWN=(deploy/README.md)

# 1. 在临时工作区里把 main 合并进 public,不影响当前检出
wt=$(mktemp -d)
git worktree add -q "$wt" public
trap 'git worktree remove --force "$wt"' EXIT
if ! git -C "$wt" merge -q --no-commit --no-ff main >/dev/null 2>&1; then
  for f in "${CNB_ONLY[@]}"; do git -C "$wt" rm -q --ignore-unmatch -- "$f" >/dev/null 2>&1 || true; done
  for f in "${PUBLIC_OWN[@]}"; do
    if git -C "$wt" diff --name-only --diff-filter=U | grep -qx "$f"; then
      git -C "$wt" checkout -q --ours -- "$f" && git -C "$wt" add -- "$f"
    fi
  done
fi
conflicts=$(git -C "$wt" diff --name-only --diff-filter=U)
# 合并结果里不能出现 CNB 内容(例如 main 在共用文件里新加了 CNB 相关代码)
leftover=$(git -C "$wt" grep -n -i 'cnb' -- . ':!testsite' || true)
if [ -n "$conflicts" ] || [ -n "$leftover" ]; then
  echo "合并 main 到 public 需要手动处理:" >&2
  [ -n "$conflicts" ] && echo "冲突:$conflicts" >&2
  [ -n "$leftover" ] && echo "出现 CNB 内容:" >&2 && echo "$leftover" >&2
  git -C "$wt" merge --abort 2>/dev/null || true
  exit 1
fi
if git -C "$wt" rev-parse -q --verify MERGE_HEAD >/dev/null; then
  git -C "$wt" commit -q --no-edit
fi

# 2. 把目录树作为新提交推到 GitHub 的对应分支(父提交为该分支在 GitHub 上的最新提交)
publish() {
  local src=$1 dst=$2 parent="" tree commit
  if git fetch -q "$REPO" "+refs/heads/$dst:refs/remotes/github/$dst" 2>/dev/null; then
    parent=$(git rev-parse "refs/remotes/github/$dst")
  fi
  tree=$(git rev-parse "$src^{tree}")
  if [ -n "$parent" ] && [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
    echo "$dst:已是最新"
    return
  fi
  commit=$(git commit-tree -S "$tree" ${parent:+-p "$parent"} -m "$msg")
  git -c credential.helper= -c credential.helper='!gh auth git-credential' push -q "$REPO" "$commit:refs/heads/$dst"
  git update-ref "refs/remotes/github/$dst" "$commit"
  echo "$dst:已同步 $(git rev-parse --short "$commit")"
}
publish main cnb
publish public main
