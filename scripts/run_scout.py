"""跑猎场扫描器，打印 top 20 表格（按"假设 5% edge 的期望$"排序）。只读。

用法:
  .venv/bin/python scripts/run_scout.py
  .venv/bin/python scripts/run_scout.py --max-markets 600 --top 20

默认扫 600 个市场（实测 ~55s）：按 volume24hr 降序分页，前 300 名的
24h 量还都在 $30k 以上，真正"冷清定价懒"的盘要翻到更深的页才出现。
"""
import argparse
import sys

sys.path.insert(0, ".")
from polymarket_agent.scout import ASSUMED_EDGE, MAX_SLIPPAGE, scout


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description="Polymarket 猎场扫描器（只读）")
    ap.add_argument("--max-markets", type=int, default=600)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    grounds = scout(max_markets=args.max_markets, top_n=args.top)
    if not grounds:
        print("没扫到候选盘（网络问题或过滤太狠）。")
        return 1

    hdr = (
        f"{'期望$':>7} {'容量$':>9} {'中间价':>6} {'spread':>7} "
        f"{'24h量$':>10} {'猎场分':>6}  question"
    )
    print(f"\n假设 edge={ASSUMED_EDGE:.0%}、滑点上限={MAX_SLIPPAGE:.0%} 的可部署容量：\n")
    print(hdr)
    print("-" * min(len(hdr) + 40, 110))
    for g in grounds:
        c = g.cand
        mid = g.book_mid if g.book_mid is not None else c.mid
        print(
            f"{g.expected_usd:>7.0f} {g.capacity_usd:>9.0f} {mid:>6.3f} "
            f"{c.spread:>7.3f} {c.volume24h:>10.0f} {c.score:>6.1f}  {_cut(c.question, 52)}"
        )
    print(
        "\n提醒：猎场分高 ≠ 有 edge。这只是'值得研究的池子'——定价懒、对手弱、"
        "容量够；每个盘仍要自己建概率模型验证后才谈买入。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
