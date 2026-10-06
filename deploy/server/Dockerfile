# 自有服务器渠道的 worker 镜像:在 .ide/Dockerfile 构建的基础镜像(FlareSolverr + cloudflared + 网关依赖)上加入代码。
# 由 deploy/install.sh 在服务器上构建,构建上下文为仓库根目录。
ARG BASE=turnstile-solver-base
FROM ${BASE}
COPY . /opt/turnstile-solver
WORKDIR /opt/turnstile-solver
# 常驻运行:不按 CNB 的 18 小时生命周期摘流量
ENV AGENT_PERMANENT=1
CMD ["python", "-m", "fleet.agent", "run"]
