"""交易日志: 从 Polymarket 公开 data-api 拉真实成交, 维护 journal.json。

只用公开 HTTP 接口 (data-api.polymarket.com), 不依赖 client.py/orders.py/私钥。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests  # 用 requests(+socksio) 而非 urllib：环境是 SOCKS 代理，urllib 不支持会 SSL EOF

WALLET = os.environ.get("POLYMARKET_WALLET", "")  # your proxy wallet address
DATA_API = "https://data-api.polymarket.com"
JOURNAL_PATH = Path(__file__).resolve().parent / "journal.json"
PAGE = 500


def _get(path: str, **params):
    last = None
    for _ in range(5):
        try:
            resp = requests.get(
                f"{DATA_API}/{path}",
                params=params,
                headers={"User-Agent": "polymarket-agent-journal/1.0"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # SOCKS 代理偶尔断，重试
            last = exc
            time.sleep(1.5)
    raise last


def _fetch_trades(taker_only: bool) -> list:
    """分页拉全部成交。takerOnly=false 是全量, takerOnly=true 只含 taker 成交。"""
    out, offset = [], 0
    while True:
        page = _get(
            "trades",
            user=WALLET,
            limit=PAGE,
            offset=offset,
            takerOnly="true" if taker_only else "false",
        )
        out.extend(page)
        if len(page) < PAGE:
            return out
        offset += PAGE


def _trade_id(t: dict) -> str:
    """唯一 id: 同一 tx 可能含多笔 fill, 用 hash+资产+方向+时间+量+价 组合去重。"""
    return "{}:{}:{}:{}:{}:{}".format(
        t.get("transactionHash", ""), t.get("asset", ""), t.get("side", ""),
        t.get("timestamp", ""), t.get("size", ""), t.get("price", ""),
    )


def _load() -> dict:
    if JOURNAL_PATH.exists():
        return json.loads(JOURNAL_PATH.read_text())
    return {"wallet": WALLET, "synced_at": None, "trades": [], "positions": []}


def sync() -> None:
    """拉全部成交+持仓, 增量合并进 journal.json。已填 thesis/review 不覆盖。"""
    all_trades = _fetch_trades(taker_only=False)
    taker_ids = {_trade_id(t) for t in _fetch_trades(taker_only=True)}
    positions = _get("positions", user=WALLET)

    journal = _load()
    existing = {rec["id"]: rec for rec in journal.get("trades", [])}
    added = 0
    for t in all_trades:
        tid = _trade_id(t)
        rec = {
            "id": tid,
            "time": datetime.fromtimestamp(t["timestamp"], tz=timezone.utc).isoformat(),
            "title": t.get("title", ""),
            "conditionId": t.get("conditionId", ""),
            "side": t.get("side", ""),
            "outcome": t.get("outcome", ""),
            "price": t.get("price"),
            "size": t.get("size"),
            "amount": round((t.get("price") or 0) * (t.get("size") or 0), 4),
            "role": "taker" if tid in taker_ids else "maker",
            "transactionHash": t.get("transactionHash", ""),
            "thesis": "",
            "review": "",
        }
        old = existing.get(tid)
        if old:
            rec["thesis"] = old.get("thesis", "")
            rec["review"] = old.get("review", "")
        else:
            added += 1
        existing[tid] = rec

    journal["wallet"] = WALLET
    journal["trades"] = sorted(existing.values(), key=lambda r: r["time"])
    journal["positions"] = [
        {k: p.get(k) for k in (
            "title", "outcome", "size", "avgPrice", "curPrice",
            "currentValue", "cashPnl", "conditionId",
        )}
        for p in positions
    ]
    journal["synced_at"] = datetime.now(timezone.utc).isoformat()
    JOURNAL_PATH.write_text(json.dumps(journal, ensure_ascii=False, indent=2))
    print(f"同步完成: 成交 {len(all_trades)} 笔 (新增 {added}), 持仓 {len(positions)} 个")
    print(f"日志文件: {JOURNAL_PATH}")


def show() -> None:
    """按时间倒序打印交易日志 + 统计。"""
    if not JOURNAL_PATH.exists():
        print("journal.json 不存在, 先运行 sync")
        return
    journal = json.loads(JOURNAL_PATH.read_text())
    trades = sorted(journal.get("trades", []), key=lambda r: r["time"], reverse=True)
    if not trades:
        print("暂无成交记录")
        return

    header = f"{'时间(UTC)':<17}{'市场':<32}{'方向':<5}{'outcome':<22}{'价':>6}{'量':>10}{'金额$':>9}  thesis"
    print(header)
    print("-" * len(header))
    for r in trades:
        t = r["time"][:16].replace("T", " ")
        title = (r["title"][:28] + "…") if len(r["title"]) > 29 else r["title"]
        outcome = (r["outcome"][:18] + "…") if len(r["outcome"]) > 19 else r["outcome"]
        thesis = r.get("thesis") or "[未填论点]"
        print(f"{t:<17}{title:<32}{r['side']:<5}{outcome:<22}"
              f"{r['price']:>6.2f}{r['size']:>10.2f}{r['amount']:>9.2f}  {thesis}")

    n = len(trades)
    buys = sum(1 for r in trades if r["side"] == "BUY")
    sells = n - buys
    takers = sum(1 for r in trades if r.get("role") == "taker")
    print("-" * len(header))
    print(f"总计 {n} 笔 | 买 {buys} / 卖 {sells} | taker 占比 {takers}/{n} ({takers / n:.0%})")
    if journal.get("synced_at"):
        print(f"上次同步: {journal['synced_at']}")
