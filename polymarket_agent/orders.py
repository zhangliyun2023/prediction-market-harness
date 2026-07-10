"""快速下单：优先市价 FOK，支持批量，不重复拉 market 元数据。

所有 BUY 路径强制硬顶 MAX_SPEND_USD（见 risk.py），不可绕过。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from py_clob_client_v2 import (
    MarketOrderArgs,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersV2Args,
    Side,
)

from polymarket_agent.risk import assert_buy_spend
from polymarket_agent.session import get_client

# 防情绪化梭哈的硬顶：单笔名义金额（price*size）绝不允许超过这个数。
# 想下更大的单？必须改这行代码——改代码本身就是冷静期。
# 教训：经历过 $5 亏损后情绪上头、$45 追高梭哈，这个常量就是为那次教训立的。
MAX_ORDER_NOTIONAL_USD = 20.0


def _check_order_notional(price: float, size: float) -> None:
    """单笔名义金额硬顶检查。必须在任何网络调用之前执行。"""
    notional = float(price) * float(size)
    if notional > MAX_ORDER_NOTIONAL_USD:
        raise ValueError(
            f"单笔名义金额 ${notional:.2f} (price={price} x size={size}) "
            f"超过硬顶 ${MAX_ORDER_NOTIONAL_USD:.2f}。"
            f"防情绪化梭哈：要下更大的单，先去改 MAX_ORDER_NOTIONAL_USD，改代码本身就是冷静期。"
        )


def _side(side: str):
    return Side.BUY if side.upper() == "BUY" else Side.SELL


def _options_for_token(client, token_id: str) -> PartialCreateOrderOptions:
    # V2 客户端会缓存 tick/neg_risk；首次按 token 拉一次即可
    tick = str(client.get_tick_size(token_id))
    neg = bool(client.get_neg_risk(token_id))
    return PartialCreateOrderOptions(tick_size=tick, neg_risk=neg)


def _buy_notional(price: float, size: float) -> float:
    return float(price) * float(size)


def build_order_args(client, condition_id: str, token_id: str, price: float, size: float, side: str):
    """兼容旧接口；condition_id 可忽略，改走 token 缓存。"""
    _check_order_notional(price, size)  # 硬顶检查先行，在 tick/neg_risk 网络调用之前
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=_side(side),
    )
    return order_args, _options_for_token(client, token_id)


def place_order(
    client,
    condition_id: str,
    token_id: str,
    price: float,
    size: float,
    side: str,
    order_type: OrderType = OrderType.GTC,
):
    _check_order_notional(price, size)  # 硬顶检查先行，在任何网络调用之前
    if str(side).upper() == "BUY":
        assert_buy_spend(_buy_notional(price, size), label="place_order BUY")
    order_args, options = build_order_args(client, condition_id, token_id, price, size, side)
    return client.create_and_post_order(
        order_args=order_args,
        options=options,
        order_type=order_type,
    )


def market_buy(
    token_id: str,
    usdc_amount: float,
    *,
    client=None,
    order_type: OrderType = OrderType.FOK,
) -> dict:
    """紧急买入：按 USDC 金额市价 FOK。硬顶 MAX_SPEND_USD。"""
    assert_buy_spend(usdc_amount, label="market_buy")
    client = client or get_client()
    t0 = time.perf_counter()
    options = _options_for_token(client, token_id)
    args = MarketOrderArgs(
        token_id=token_id,
        amount=float(usdc_amount),
        side=Side.BUY,
        order_type=order_type,
    )
    resp = client.create_and_post_market_order(
        order_args=args,
        options=options,
        order_type=order_type,
    )
    ms = (time.perf_counter() - t0) * 1000
    return {"response": resp, "elapsed_ms": ms, "side": "BUY", "amount": usdc_amount}


def market_sell(
    token_id: str,
    shares: float,
    *,
    client=None,
    order_type: OrderType = OrderType.FOK,
) -> dict:
    """紧急卖出：按份额市价 FOK。"""
    client = client or get_client()
    t0 = time.perf_counter()
    options = _options_for_token(client, token_id)
    # MarketOrderArgs.amount 对 SELL 是份额数
    args = MarketOrderArgs(
        token_id=token_id,
        amount=float(shares),
        side=Side.SELL,
        order_type=order_type,
    )
    resp = client.create_and_post_market_order(
        order_args=args,
        options=options,
        order_type=order_type,
    )
    ms = (time.perf_counter() - t0) * 1000
    return {"response": resp, "elapsed_ms": ms, "side": "SELL", "amount": shares}


def limit_order(
    token_id: str,
    price: float,
    size: float,
    side: str,
    *,
    client=None,
    order_type: OrderType = OrderType.GTC,
) -> dict:
    if str(side).upper() == "BUY":
        assert_buy_spend(_buy_notional(price, size), label="limit_order BUY")
    client = client or get_client()
    t0 = time.perf_counter()
    order_args, options = build_order_args(client, "", token_id, price, size, side)
    resp = client.create_and_post_order(
        order_args=order_args,
        options=options,
        order_type=order_type,
    )
    return {"response": resp, "elapsed_ms": (time.perf_counter() - t0) * 1000}


def batch_limit_orders(
    legs: list[dict],
    *,
    client=None,
    order_type: OrderType = OrderType.FOK,
    parallel_sign: bool = True,
) -> dict:
    """批量限价单：并行签名，一次 post_orders。

    legs: [{token_id, price, size, side}]
    BUY 合计名义金额硬顶 MAX_SPEND_USD。
    """
    buy_notional = sum(
        _buy_notional(float(leg["price"]), float(leg["size"]))
        for leg in legs
        if str(leg.get("side", "BUY")).upper() == "BUY"
    )
    if buy_notional > 0:
        assert_buy_spend(buy_notional, label="batch_limit_orders BUY total")

    client = client or get_client()
    t0 = time.perf_counter()

    def _sign(leg: dict):
        token_id = str(leg["token_id"])
        order_args, options = build_order_args(
            client,
            "",
            token_id,
            float(leg["price"]),
            float(leg["size"]),
            str(leg.get("side", "BUY")),
        )
        signed = client.create_order(order_args, options)
        return PostOrdersV2Args(order=signed, orderType=order_type)

    if parallel_sign and len(legs) > 1:
        signed_args: list[Any] = [None] * len(legs)
        with ThreadPoolExecutor(max_workers=min(12, len(legs))) as pool:
            futs = {pool.submit(_sign, leg): i for i, leg in enumerate(legs)}
            for fut in as_completed(futs):
                signed_args[futs[fut]] = fut.result()
    else:
        signed_args = [_sign(leg) for leg in legs]

    t_sign = time.perf_counter()
    resp = client.post_orders(signed_args)
    t_end = time.perf_counter()
    return {
        "response": resp,
        "sign_ms": (t_sign - t0) * 1000,
        "post_ms": (t_end - t_sign) * 1000,
        "elapsed_ms": (t_end - t0) * 1000,
        "n": len(legs),
    }
