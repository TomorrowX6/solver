# 服务器渠道的 worker 镜像:在 .ide/Dockerfile 构建的基础镜像(FlareSolverr + cloudflared + 网关依赖)上加入代码。
# GitHub Actions 用 docker-bake.hcl 构建并推送到 ghcr.io;构建上下文为仓库根目录。
ARG BASE=turnstile-solver-base
FROM ${BASE}
COPY . /opt/turnstile-solver
WORKDIR /opt/turnstile-solver
CMD ["python", "-m", "fleet.agent", "run"]
