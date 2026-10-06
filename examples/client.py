"""调用示例:python examples/client.py [--url URL] [--session NAME] [--api-key KEY]

请求与响应格式同 FlareSolverr /v1。默认打开 https://nowsecure.nl/(一个带 Cloudflare 验证的公开测试页)。
"""

from __future__ import annotations

import argparse
import json

import httpx


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--url", default="https://nowsecure.nl/")
    p.add_argument("--api-key")
    p.add_argument("--session", help="复用浏览器会话(同名会话的 cookie 会保留)")
    p.add_argument("--tabs", type=int, help="页面内有 Turnstile 组件时,按几次 Tab 聚焦到组件(tabs_till_verify)")
    args = p.parse_args()

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    payload = {"cmd": "request.get", "url": args.url, "maxTimeout": 60000}
    if args.session:
        payload["session"] = args.session
    if args.tabs is not None:
        payload["tabs_till_verify"] = args.tabs

    with httpx.Client(base_url=args.base, headers=headers, timeout=120) as client:
        resp = client.post("/v1", json=payload)
        data = resp.json()
    if data.get("status") != "ok":
        print(f"HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)}")
        return
    solution = data["solution"]
    print("status:", solution["status"])
    print("userAgent:", solution["userAgent"])
    print("cookies:", {c["name"]: c["value"][:24] for c in solution.get("cookies", [])})
    if solution.get("turnstile_token"):
        print("turnstile_token:", solution["turnstile_token"][:40] + "…")
    print("html:", len(solution.get("response") or ""), "chars")


if __name__ == "__main__":
    main()
