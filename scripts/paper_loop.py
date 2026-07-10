"""自动 paper 循环：定时 record + settle，攒样本验证信号有没有真 edge。

不碰真钱、不用私钥。每轮扫当天 BTC 阈值盘、记录 actionable 信号、结算到期持仓，
把每轮状态追加到日志。一轮出错不影响下一轮。

用法: python scripts/paper_loop.py [间隔秒 默认900] [最长小时 默认72]
建议后台跑: python scripts/paper_loop.py &
想跨重启长期跑，用 launchd/cron（问我要配置）。
"""
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")
from polymarket_agent.signal import scan
from polymarket_agent import paper


def today_slug() -> str:
    """当天 BTC 阈值盘 slug，形如 bitcoin-above-on-july-9-2026（UTC 日期）。"""
    now = datetime.now(timezone.utc)
    month = now.strftime("%B").lower()
    return f"bitcoin-above-on-{month}-{now.day}-{now.year}"


def one_cycle():
    slug = today_slug()
    res = scan(slug)
    if res.get("error"):
        return f"scan 无数据({slug}): {res['error']}"
    new = paper.record(res)
    settled = paper.settle()
    ledger = paper._load()
    opens = [p for p in ledger["positions"] if p["status"] == "open"]
    done = [p for p in ledger["positions"] if p["status"] == "settled"]
    pnl = sum(p.get("pnl", 0.0) for p in done)
    return (f"slug={slug} spot=${res['spot']:,.0f} vol={res['sigma_annual']*100:.0f}% "
            f"新记{len(new)} 新结算{len(settled)} | 未平仓{len(opens)} 已结算{len(done)} 累计P&L=${pnl:+.2f}")


def main():
    # once 模式：跑单轮就退出。给 launchd 用——每 15 分钟由 launchd 触发一次，
    # 关窗口/崩溃/重启都能自愈（launchd 下次自动再触发），比长驻进程稳。
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        try:
            msg = one_cycle()
        except Exception as e:
            msg = f"本轮出错(跳过): {type(e).__name__} {e}"
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)
        return
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    max_hours = float(sys.argv[2]) if len(sys.argv) > 2 else 72
    start = time.time()
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] paper 循环启动，每 {interval}s 一轮，最长 {max_hours}h", flush=True)
    while (time.time() - start) < max_hours * 3600:
        try:
            msg = one_cycle()
        except Exception as e:
            msg = f"本轮出错(跳过): {type(e).__name__} {e}"
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)
        time.sleep(interval)
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] 跑满 {max_hours}h，退出。", flush=True)


if __name__ == "__main__":
    main()
