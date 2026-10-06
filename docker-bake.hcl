# 服务器渠道镜像:先用 .ide/Dockerfile 构建基础镜像(FlareSolverr + cloudflared + 网关依赖),
# 再由 deploy/server/Dockerfile 加入代码。GitHub Actions 用 docker buildx bake 构建并推送到 ghcr.io。
variable "IMAGE" { default = "ghcr.io/tomorrowx6/solver" }
variable "SHA" { default = "dev" }

target "base" {
  context    = ".ide"
  dockerfile = "Dockerfile"
  platforms  = ["linux/amd64", "linux/arm64"]
}

target "worker" {
  context    = "."
  dockerfile = "deploy/server/Dockerfile"
  contexts   = { "turnstile-solver-base" = "target:base" }
  args       = { BASE = "turnstile-solver-base" }
  platforms  = ["linux/amd64", "linux/arm64"]
  tags       = ["${IMAGE}:latest", "${IMAGE}:${SHA}"]
  labels     = { "org.opencontainers.image.source" = "https://github.com/TomorrowX6/solver" }
}

group "default" { targets = ["worker"] }
