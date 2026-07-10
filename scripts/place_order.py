"""真下单，会花钱。用法:
python scripts/place_order.py <condition_id> <token_id> <price> <size> <BUY|SELL>
下单前先强制回答三问（任何一问空答案直接取消），再敲 yes 二次确认，不会自动执行。
三问答案连同下单参数追加记入 polymarket_agent/trade_notes.json。
紧急场景请用: scripts/fast_trade.py
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from polymarket_agent.orders import place_order
from polymarket_agent.session import warm

TRADE_NOTES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "polymarket_agent",
    "trade_notes.json",
)

THREE_QUESTIONS = [
    ("edge_source", "1) 这单的 edge 来源是什么？(一句话): "),
    ("counterparty", "2) 对手方是谁，他为什么错？: "),
    ("pct_of_account", "3) 本单金额占账户总资产的百分之几？: "),
]


def ask_three_questions() -> dict | None:
    """下单前三问。任何一问答案为空 -> 返回 None（取消下单）。"""
    answers = {}
    for key, prompt in THREE_QUESTIONS:
        ans = input(prompt).strip()
        if not ans:
            print("有一问答不上来——想不清楚就不该下这单。已取消，没有下单。")
            return None
        answers[key] = ans
    return answers


def append_trade_note(note: dict) -> None:
    """追加写入 trade_notes.json（数组，不存在则创建）。"""
    notes = []
    if os.path.exists(TRADE_NOTES_PATH):
        try:
            with open(TRADE_NOTES_PATH, "r") as f:
                notes = json.load(f)
            if not isinstance(notes, list):
                notes = []
        except (json.JSONDecodeError, OSError):
            notes = []
    notes.append(note)
    with open(TRADE_NOTES_PATH, "w") as f:
        json.dump(notes, f, indent=2, ensure_ascii=False)


def main():
    if len(sys.argv) != 6:
        print(__doc__)
        return

    condition_id, token_id, price, size, side = sys.argv[1:]
    price = float(price)
    size = float(size)

    print(f"即将下单: {side} {size} @ {price}  token={token_id}")
    print("下单前三问（防情绪化交易，任何一问空答案直接取消）:")
    answers = ask_three_questions()
    if answers is None:
        return

    append_trade_note({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "order": {
            "condition_id": condition_id,
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
        },
        **answers,
    })
    print(f"三问已记录: {TRADE_NOTES_PATH}")

    confirm = input("确认真实下单？输入 yes 继续，其他任意键取消: ")
    if confirm.strip().lower() != "yes":
        print("已取消，没有下单。")
        return

    client = warm()
    response = place_order(client, condition_id, token_id, price, size, side)
    print("Order ID:", response.get("orderID"))
    print("Status:", response.get("status"))


if __name__ == "__main__":
    main()
