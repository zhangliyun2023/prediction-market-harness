"""信号层：币安现价推的模型概率 vs Polymarket 隐含概率，偏离即信号。

只算不下单。返回结构化信号，交给上层（run_signal.py）打印或 paper 记账。
"""
import json
import re
import time
from datetime import datetime, timezone

import requests

from polymarket_agent import binance

GAMMA_HOST = "https://gamma-api.polymarket.com"


def _parse_enddate(iso: str) -> float:
    """ISO 时间 -> epoch 秒。"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).timestamp()


def fetch_above_markets(slug: str):
    """拉一个 'Bitcoin above ___ on ...' 事件下的所有阈值市场。"""
    resp = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None, []
    event = data[0]
    end_ts = _parse_enddate(event["endDate"])
    rows = []
    for m in event.get("markets", []):
        mm = re.search(r"above \$([\d,]+)", m.get("question", ""))
        if not mm:
            continue
        strike = float(mm.group(1).replace(",", ""))
        prices = json.loads(m["outcomePrices"])
        rows.append({
            "question": m["question"],
            "strike": strike,
            "market_prob": float(prices[0]),   # Yes = 收盘价高于 strike
            "conditionId": m.get("conditionId"),
            "clobTokenIds": json.loads(m.get("clobTokenIds", "[]")),
        })
    return end_ts, rows


def scan(slug: str, symbol: str = "BTCUSDT", edge_threshold: float = 0.05):
    """对比模型概率与市场概率，返回按 |edge| 排序的信号。

    edge = 模型概率 - 市场概率。正 = 市场低估 Yes（模型觉得该更贵），可考虑买 Yes。
    """
    end_ts, rows = fetch_above_markets(slug)
    if not rows:
        return {"error": "无阈值市场", "signals": []}

    spot = binance.spot_price(symbol)
    sigma = binance.annualized_vol(symbol)
    now = time.time()
    ttl = end_ts - now

    signals = []
    for r in rows:
        model_p = binance.prob_above(spot, r["strike"], sigma, ttl)
        edge = model_p - r["market_prob"]
        signals.append({
            **r,
            "model_prob": model_p,
            "edge": edge,
            "actionable": abs(edge) >= edge_threshold,
        })
    signals.sort(key=lambda s: -abs(s["edge"]))
    return {
        "spot": spot,
        "sigma_annual": sigma,
        "seconds_to_settle": ttl,
        "signals": signals,
    }
