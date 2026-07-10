"""币安现货价格 + 波动率，只读，不需要密钥。信号层的"真值"来源。"""
import math
import time

import requests

# data-api.binance.vision 是币安公开只读镜像，无地域限制、无需 key
BINANCE_HOST = "https://data-api.binance.vision"


def spot_price(symbol: str = "BTCUSDT") -> float:
    """当前现货价。"""
    resp = requests.get(
        f"{BINANCE_HOST}/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["price"])


def annualized_vol(
    symbol: str = "BTCUSDT",
    interval: str = "5m",
    lookback: int = 288,
    halflife_bars: float = 36.0,
) -> float:
    """EWMA 年化波动率，默认用最近 24h 的 5m K 线。

    为什么改成这样（针对"几小时内结算"的短线盘）：
    - 原实现用一周(168根)小时线做等权样本标准差，窗口太长、反应太慢：
      前几天的平静/剧烈行情会一直压/抬当前估计，等结算只剩几小时时用它明显失真。
    - 改用更高频的 5m K 线(24h=288根)捕捉当下的分钟级波动结构；
    - 再叠加 EWMA(指数加权)：越近的收益权重越大(RiskMetrics 思路)，
      让估计对"刚刚变没变"快速响应，同时保留足够样本压噪声。
    - halflife_bars=36 根 5m ≈ 3 小时，正好匹配这类盘的结算尺度。

    EWMA 方差用零均值假设(短窗口高频下漂移可忽略、且更稳)：
      var = Σ w_i * r_i^2 / Σ w_i,  w_i = lambda^(age)，age 越小(越近)权重越大。
    可选参数都有默认值，signal.py 的 annualized_vol(symbol) 单参调用不受影响。
    """
    resp = requests.get(
        f"{BINANCE_HOST}/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": lookback + 1},
        timeout=10,
    )
    resp.raise_for_status()
    closes = [float(k[4]) for k in resp.json()]
    if len(closes) < 3:
        raise RuntimeError("K线数据不足，无法估波动率")

    # 按时间顺序的对数收益（rets[-1] 是最新一根）
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    # EWMA 衰减因子：半衰期 halflife_bars 根 -> lambda = 0.5^(1/halflife)
    lam = 0.5 ** (1.0 / halflife_bars)
    # age=0 给最新一根，越老权重越小
    weighted_sq = 0.0
    weight_sum = 0.0
    for age, r in enumerate(reversed(rets)):
        w = lam ** age
        weighted_sq += w * r * r
        weight_sum += w
    sigma_per_bar = math.sqrt(weighted_sq / weight_sum)

    # 每年的 bar 数：5m -> 12*24*365
    bars_per_year = {"1m": 525600, "5m": 105120, "15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}
    n = bars_per_year.get(interval, 8760)
    return sigma_per_bar * math.sqrt(n)


def prob_above(spot: float, strike: float, sigma_annual: float, seconds_to_settle: float) -> float:
    """无漂移对数正态模型下，结算时 价格 > strike 的概率。

    P(S_T > K) = N(d2), d2 = (ln(S0/K) - 0.5*sigma^2*T) / (sigma*sqrt(T))
    T 用年为单位。已结算(T<=0)时退化为确定性比较。
    """
    T = seconds_to_settle / (365 * 24 * 3600)
    if T <= 0 or sigma_annual <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) - 0.5 * sigma_annual ** 2 * T) / (sigma_annual * math.sqrt(T))
    return 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
