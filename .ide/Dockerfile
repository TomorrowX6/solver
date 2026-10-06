# CNB 云原生开发环境(仅预览模式)使用的 worker 镜像:FlareSolverr + 网关 + cloudflared。
# 由 .cnb.yml 的 docker.build 构建并缓存;构建上下文中只有本文件,不要 COPY 仓库文件。
FROM flaresolverr/flaresolverr:v3.5.2

# 上游镜像以 flaresolverr 用户运行;CNB 环境内统一用 root 启动各进程(Chrome 已带 --no-sandbox)
USER root
ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# waitress 默认只有 4 个线程,单实例最多同时处理 4 个请求;改为由 WAITRESS_THREADS 控制。
# 上游这一行变动时 grep 会失败,构建随之失败,避免补丁悄悄失效
RUN sed -i 's/serve(handler, host=self.host, port=self.port, asyncore_use_poll=True)/serve(handler, host=self.host, port=self.port, asyncore_use_poll=True, threads=int(os.environ.get("WAITRESS_THREADS", "4")))/' /app/flaresolverr.py \
 && grep -q 'WAITRESS_THREADS' /app/flaresolverr.py

# FlareSolverr 的浏览器以空白页启动:默认新标签页会请求 Google(国内网络下挂起),chromedriver 要等它结束才开始导航,
# 导致约 1/5 的 /v1 请求卡在打开页面直到超时。网关进程已在 ChromeOptions 里加了 about:blank,这里判断后不重复添加
RUN sed -i 's/^    options = uc.ChromeOptions()$/    options = uc.ChromeOptions()\n    if "about:blank" not in options.arguments:\n        options.add_argument("about:blank")/' /app/utils.py \
 && grep -q 'options.add_argument("about:blank")' /app/utils.py

# 按机器架构下载(CNB 为 amd64;自有服务器也可能是 arm64)
RUN curl -fsSL --retry 3 -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture)" \
 && chmod +x /usr/local/bin/cloudflared \
 && cloudflared --version

# 网关依赖,装进上游镜像自带的 Python 3.11
RUN pip install "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27" "pydantic>=2.6"
