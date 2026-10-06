"""CNB OpenAPI 的最小客户端,只用标准库,方便在流水线里免安装运行。"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class CnbError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class CnbClient:
    def __init__(self, token: str, repo: str, base: str = "https://api.cnb.cool", timeout: float = 30) -> None:
        self._token = token
        self.repo = repo.strip("/")
        self._base = base.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> Any:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"

        last: Exception | None = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code < 500:
                    raise CnbError(e.code, detail) from None
                last = CnbError(e.code, detail)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
                # 连接被重置、对端提前断开等瞬时错误,重试
                last = e
            time.sleep(1.5 * (attempt + 1))
        assert last is not None
        raise last

    def list_running_workspaces(self, branch: str | None = None, all_repos: bool = False) -> list[dict]:
        """当前用户运行中的开发环境;all_repos=True 时不限仓库(账号级并发上限按全部环境计算)。"""
        result: list[dict] = []
        page = 1
        while True:
            query = {"status": "running", "page": page, "page_size": 100}
            if not all_repos:
                query.update(slug=self.repo, branch=branch)
            data = self._request("GET", "/workspace/list", query=query)
            result.extend(data.get("list") or [])
            if not data.get("hasMore"):
                return result
            page += 1

    def build_status(self, sn: str) -> dict:
        return self._request("GET", f"/{self.repo}/-/build/status/{sn}")

    def build_info(self, sn: str) -> dict | None:
        """构建记录,含 event、title、sha 等字段。"""
        data = self._request("GET", f"/{self.repo}/-/build/logs", query={"sn": sn, "page_size": 1})
        builds = (data or {}).get("data") or []
        return builds[0] if builds else None

    def start_build(self, branch: str, event: str, title: str, env: dict[str, str] | None = None) -> dict:
        body: dict = {"branch": branch, "event": event, "title": title, "sync": "false"}
        if env:
            body["env"] = env
        return self._request("POST", f"/{self.repo}/-/build/start", body=body)

    def stop_workspace(self, sn: str) -> dict:
        return self._request("POST", "/workspace/stop", body={"sn": sn})
