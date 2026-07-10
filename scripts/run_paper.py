"""Paper（纸上模拟）交易命令行入口。绝不真实下单、不碰私钥。

用法:
  python scripts/run_paper.py record  [slug] [stake]  # 跑一次 scan 并记录 actionable 信号
  python scripts/run_paper.py settle  [slug]          # 结算已过期持仓（用币安现价近似）
  python scripts/run_paper.py summary                 # 打印账本汇总

默认 slug: bitcoin-above-on-july-9-2026
"""
import sys

sys.path.insert(0, ".")
from polymarket_agent import paper
from polymarket_agent.signal import scan

DEFAULT_SLUG = "bitcoin-above-on-july-9-2026"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"

    if cmd == "record":
        slug = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SLUG
        stake = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
        res = scan(slug)
        if res.get("error"):
            print("扫描错误:", res["error"])
            return
        new = paper.record(res, stake=stake)
        print(f"扫描 {slug}: 现价 ${res['spot']:,.0f}, {len(res['signals'])} 个市场, "
              f"actionable {sum(1 for s in res['signals'] if s['actionable'])} 个")
        print(f"本次新记 {len(new)} 条纸上持仓:")
        for p in new:
            print(f"  买 {p['side']} @ {p['entry_price']:.3f}  strike ${p['strike']:,.0f}  "
                  f"{p['shares']:.2f} 股 / ${p['stake']:.0f}")
        if not new:
            print("  (无新持仓：可能没有 actionable 信号，或都已记录过)")

    elif cmd == "settle":
        symbol = "BTCUSDT"
        done = paper.settle(symbol=symbol)
        print(f"本次结算 {len(done)} 条持仓。")
        for p in done:
            print(f"  strike ${p['strike']:,.0f} {p['side']}: 赢方 {p['winning_side']}, "
                  f"{'赢' if p['won'] else '输'}, 盈亏 {p['pnl']:+.2f}")
        paper.summary()

    elif cmd == "summary":
        paper.summary()

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
