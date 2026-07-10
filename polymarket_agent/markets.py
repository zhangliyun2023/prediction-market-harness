"""公开行情接口，不需要密钥。"""
from __future__ import annotations

import json
import time
from typing import Any

import requests

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"


def _get(url: str, params: dict | None = None, retries: int = 4, timeout: int = 30) -> Any:
    last: Exception | None = None
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — network retry wrapper
            last = exc
            time.sleep(1.2 * (i + 1))
    assert last is not None
    raise last


def fetch_active_markets(limit: int = 10, offset: int = 0):
    return _get(
        f"{GAMMA_HOST}/markets",
        {"active": "true", "closed": "false", "limit": limit, "offset": offset},
    )


def fetch_market_by_slug(slug: str):
    markets = _get(f"{GAMMA_HOST}/markets", {"slug": slug})
    return markets[0] if markets else None


def fetch_active_events(limit: int = 100, offset: int = 0, order: str = "volume24hr"):
    return _get(
        f"{GAMMA_HOST}/events",
        {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": "false",
        },
    )


def fetch_event_by_slug(slug: str):
    events = _get(f"{GAMMA_HOST}/events", {"slug": slug})
    if isinstance(events, list):
        return events[0] if events else None
    return events


def iter_active_events(max_events: int = 2000, page_size: int = 100):
    offset = 0
    while offset < max_events:
        batch = fetch_active_events(limit=min(page_size, max_events - offset), offset=offset)
        if not batch:
            break
        for event in batch:
            yield event
        if len(batch) < page_size:
            break
        offset += page_size


def parse_json_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def fetch_order_book(token_id: str) -> dict:
    return _get(f"{CLOB_HOST}/book", {"token_id": token_id})


def best_levels(book: dict) -> tuple[dict | None, dict | None]:
    """Return (best_ask, best_bid) as {price, size} dicts."""
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    best_ask = min(asks, key=lambda x: float(x["price"])) if asks else None
    best_bid = max(bids, key=lambda x: float(x["price"])) if bids else None
    return best_ask, best_bid
