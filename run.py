from __future__ import annotations

import argparse
import dataclasses
import logging
import sys

import uvicorn

from app.config import Settings
from app.main import create_app


def main() -> None:
    base = Settings.from_env()
    p = argparse.ArgumentParser(description="FlareSolverr gateway")
    p.add_argument("--host", default=base.host)
    p.add_argument("--port", type=int, default=base.port)
    p.add_argument("--backend-url", default=base.backend_url, help="FlareSolverr 地址")
    p.add_argument("--concurrency", type=int, default=base.max_concurrency)
    p.add_argument("--queue", type=int, default=base.max_queue)
    p.add_argument("--api-key", default=base.api_key)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    settings = dataclasses.replace(
        base,
        host=args.host,
        port=args.port,
        backend_url=args.backend_url,
        max_concurrency=args.concurrency,
        max_queue=args.queue,
        api_key=args.api_key,
    )

    # Windows 下重定向到文件时默认是 GBK,统一成 UTF-8 避免中文日志乱码
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # 并发与会话状态在进程内,只能单进程运行
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
