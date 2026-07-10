"""跑币安->Polymarket 信号，只打印，绝不下单。

用法: python scripts/run_signal.py [event_slug]
默认扫当天 BTC 阈值盘。
"""
import sys

sys.path.insert(0, ".")
from polymarket_agent.signal import scan


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else "bitcoin-above-on-july-9-2026"
    res = scan(slug)
    if res.get("error"):
        print("错误:", res["error"])
        return

    ttl_min = res["seconds_to_settle"] / 60
    print(f"币安现价: ${res['spot']:,.0f}  年化波动率: {res['sigma_annual']*100:.1f}%  距结算: {ttl_min:.0f} 分钟")
    print(f"{'strike':>8} | {'市场P':>7} | {'模型P':>7} | {'edge':>7} | 信号")
    print("-" * 55)
    for s in res["signals"]:
        flag = ""
        if s["actionable"]:
            flag = "买Yes" if s["edge"] > 0 else "买No"
        print(f"{s['strike']:>8,.0f} | {s['market_prob']:>7.3f} | {s['model_prob']:>7.3f} | {s['edge']:>+7.3f} | {flag}")

    print("\n注意: 这是模型 vs 市场的偏离，不是稳赚信号。模型假设无漂移对数正态，")
    print("真实 BTC 有跳空/趋势，波动率估计也有误差。仅供 paper 观察，勿据此真下单。")


if __name__ == "__main__":
    main()
