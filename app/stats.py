"""求解记录:最近的求解明细与按分钟的计数,供后台面板(GET /admin/stats)展示。

只保存在进程内,worker 重启后清零;面板汇总所有 worker 的数据。
"""

from __future__ import annotations

import time
from collections import deque
from urllib.parse import urlsplit


class SolveLog:
    def __init__(self, recent: int = 100, keep_minutes: int = 180) -> None:
        self._recent: deque[dict] = deque(maxlen=recent)
        self._keep_minutes = keep_minutes
        # 分钟起点(epoch 秒)-> [成功数, 失败数, 成功耗时之和]
        self._minutes: dict[int, list[float]] = {}

    def record(
        self,
        url: str,
        sitekey: str,
        ok: bool,
        elapsed: float,
        attempts: int,
        error: str = "",
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self._recent.append(
            {
                "at": round(now, 1),
                "host": urlsplit(url).hostname or "",
                "sitekey": sitekey,
                "ok": ok,
                "elapsed": round(elapsed, 1),
                "attempts": attempts,
                "error": error[:200],
            }
        )
        minute = int(now // 60) * 60
        bucket = self._minutes.setdefault(minute, [0, 0, 0.0])
        if ok:
            bucket[0] += 1
            bucket[2] += elapsed
        else:
            bucket[1] += 1
        cutoff = minute - self._keep_minutes * 60
        for old in [m for m in self._minutes if m < cutoff]:
            del self._minutes[old]

    def snapshot(self) -> dict:
        """recent 按时间倒序;minutes 为 [分钟起点, 成功数, 失败数, 成功耗时之和],按时间正序。"""
        return {
            "recent": list(reversed(self._recent)),
            "minutes": [[m, int(v[0]), int(v[1]), round(v[2], 1)] for m, v in sorted(self._minutes.items())],
        }
