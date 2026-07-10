"""常驻交易会话：客户端只建一次，紧急下单时复用。"""
from __future__ import annotations

import threading
import time
from typing import Any

from polymarket_agent.client import build_client

_lock = threading.Lock()
_client = None
_warmed_at = 0.0


def get_client(force_refresh: bool = False):
    """返回已认证的 CLOB V2 客户端（进程内单例）。"""
    global _client, _warmed_at
    with _lock:
        if _client is None or force_refresh:
            t0 = time.perf_counter()
            _client = build_client()
            _warmed_at = time.time()
            print(f"[session] client ready in {(time.perf_counter() - t0) * 1000:.0f}ms")
        return _client


def warm() -> Any:
    """预热：建连 + 派生 API key，之后下单不再付这段延迟。"""
    return get_client(force_refresh=False)


def reset() -> None:
    global _client, _warmed_at
    with _lock:
        _client = None
        _warmed_at = 0.0
