"""纸上（paper）交易记账层：验证信号策略赚不赚，绝不碰真钱、不碰私钥。

不 import client/orders，不签名、不下单。只把信号层的 actionable 信号
按当前市场概率"模拟买入"，落到本地 JSON 账本，等结算时用币安现价近似判定输赢。

账本结构 (paper_ledger.json):
{
  "positions": [
    {
      "conditionId": "0x...", "strike": 62000.0, "question": "...",
      "side": "YES"|"NO",          # 买的方向
      "entry_price": 0.885,         # 买入价（YES=market_prob, NO=1-market_prob）
      "shares": 11.30,              # stake / entry_price，赢则每股兑 $1
      "stake": 10.0,
      "placed_at": "2026-07-09T...",# 下单时间
      "settle_ts": 1752...,         # 结算时间戳（epoch 秒）
      "status": "open"|"settled",
      # 结算后追加：
      "settle_spot": 63000.0, "winning_side": "YES",
      "won": true, "payout": 11.30, "pnl": 1.30, "settled_at": "..."
    }
  ]
}
"""
import json
import math
import os
import time
from datetime import datetime, timezone

from polymarket_agent import binance

# 默认本地账本；PAPER_LEDGER_PATH 环境变量可覆盖(云端 GitHub Actions 用独立账本，
# 避免和本地 launchd 写同一个文件打架)。
LEDGER_PATH = os.environ.get("PAPER_LEDGER_PATH") or os.path.join(
    os.path.dirname(__file__), "paper_ledger.json"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: str = LEDGER_PATH) -> dict:
    if not os.path.exists(path):
        return {"positions": []}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "positions" not in data:
            return {"positions": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"positions": []}


def _save(ledger: dict, path: str = LEDGER_PATH) -> None:
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)


def record(scan_result: dict, stake: float = 10.0, path: str = LEDGER_PATH) -> list:
    """对每个 actionable 信号模拟买入，写入账本。返回本次新记的持仓列表。

    edge>0 -> 模型觉得 Yes 被低估 -> 买 YES（价=market_prob）
    edge<0 -> 模型觉得 Yes 被高估 -> 买 NO（价=1-market_prob）
    同一 conditionId+side 已有未平仓持仓则跳过，避免重复记录。
    """
    ledger = _load(path)
    # 结算时间戳：现价扫描时点 + 距结算秒数（近似，scan 不返回绝对 endDate）
    settle_ts = time.time() + scan_result.get("seconds_to_settle", 0)

    open_keys = {
        (p["conditionId"], p["side"])
        for p in ledger["positions"]
        if p["status"] == "open"
    }

    new_positions = []
    for s in scan_result.get("signals", []):
        if not s.get("actionable"):
            continue
        side = "YES" if s["edge"] > 0 else "NO"
        key = (s["conditionId"], side)
        if key in open_keys:
            continue  # 已有同方向未平仓，去重

        entry_price = s["market_prob"] if side == "YES" else 1.0 - s["market_prob"]
        if entry_price <= 0:
            continue  # 价格无效，跳过（避免除零）

        pos = {
            "conditionId": s["conditionId"],
            "strike": s["strike"],
            "question": s.get("question", ""),
            "side": side,
            "entry_price": round(entry_price, 4),
            "shares": round(stake / entry_price, 4),
            "stake": stake,
            "placed_at": _now_iso(),
            "settle_ts": settle_ts,
            "status": "open",
        }
        ledger["positions"].append(pos)
        open_keys.add(key)
        new_positions.append(pos)

    _save(ledger, path)
    return new_positions


def settle(spot_price_at_settle: float = None, symbol: str = "BTCUSDT",
           path: str = LEDGER_PATH) -> list:
    """对已过结算时间的未平仓持仓判定输赢并计已实现盈亏。

    结算价：真实结算应取 endDate 时刻的收盘价；这里用币安"当前"现价近似
    （paper 观察够用，不追求精确）。可传 spot_price_at_settle 覆盖。
    above $strike -> YES 赢，赢方每股兑 $1、输方兑 $0。
    返回本次结算的持仓列表。
    """
    ledger = _load(path)
    now = time.time()
    spot = spot_price_at_settle
    settled = []

    for p in ledger["positions"]:
        if p["status"] != "open":
            continue
        if now < p["settle_ts"]:
            continue  # 还没到结算时间
        if spot is None:
            spot = binance.spot_price(symbol)  # 近似结算价，只拉一次

        winning_side = "YES" if spot > p["strike"] else "NO"
        won = p["side"] == winning_side
        payout = round(p["shares"] * 1.0, 4) if won else 0.0

        p["status"] = "settled"
        p["settle_spot"] = spot
        p["winning_side"] = winning_side
        p["won"] = won
        p["payout"] = payout
        p["pnl"] = round(payout - p["stake"], 4)
        p["settled_at"] = _now_iso()
        settled.append(p)

    _save(ledger, path)
    return settled


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple:
    """胜率的 Wilson score 95% 置信区间，纯公式实现，不引第三方库。"""
    if n == 0:
        return 0.0, 1.0
    phat = wins / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def summary(path: str = LEDGER_PATH) -> None:
    """打印未平仓持仓、已结算盈亏、累计 P&L、胜率。"""
    ledger = _load(path)
    positions = ledger["positions"]
    opens = [p for p in positions if p["status"] == "open"]
    dones = [p for p in positions if p["status"] == "settled"]

    print("=" * 70)
    print(f"Paper 账本: {path}")
    print(f"总持仓 {len(positions)}  |  未平仓 {len(opens)}  |  已结算 {len(dones)}")

    if opens:
        print("\n-- 未平仓 --")
        print(f"{'strike':>9} | {'方向':>4} | {'买入价':>7} | {'股数':>8} | {'本金$':>6}")
        for p in opens:
            print(f"{p['strike']:>9,.0f} | {p['side']:>4} | {p['entry_price']:>7.3f} "
                  f"| {p['shares']:>8.2f} | {p['stake']:>6.1f}")

    if dones:
        print("\n-- 已结算 --")
        print(f"{'strike':>9} | {'方向':>4} | {'结算价':>10} | {'赢方':>4} | {'输赢':>4} | {'盈亏$':>8}")
        for p in dones:
            print(f"{p['strike']:>9,.0f} | {p['side']:>4} | {p['settle_spot']:>10,.0f} "
                  f"| {p['winning_side']:>4} | {'赢' if p['won'] else '输':>3} | {p['pnl']:>+8.2f}")

        wins = sum(1 for p in dones if p["won"])
        total_pnl = sum(p["pnl"] for p in dones)
        total_stake = sum(p["stake"] for p in dones)
        roi = (total_pnl / total_stake * 100) if total_stake else 0.0
        print(f"\n累计已实现 P&L: {total_pnl:+.2f} 美元  (投入 {total_stake:.0f}, ROI {roi:+.1f}%)")
        print(f"胜率: {wins}/{len(dones)} = {wins / len(dones) * 100:.0f}%")
        lo, hi = _wilson_ci(wins, len(dones))
        print(f"胜率 Wilson 95% 置信区间: [{lo * 100:.1f}%, {hi * 100:.1f}%]  (n={len(dones)})")
    else:
        print("\n(尚无已结算持仓，跑 settle 后再看盈亏/胜率)")

    n = len(dones)
    if n < 20:
        print("\n" + "!" * 70)
        print(f"!!  样本不足20(当前 n={n})，当前盈亏是噪音，不构成任何结论  !!")
        print("!" * 70)
    else:
        print(f"\n提示: Wilson 置信区间下限 = {lo * 100:.1f}%，下限 > 50% 才勉强算信号。")
    print("=" * 70)
