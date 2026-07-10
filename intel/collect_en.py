"""英文公开源采集 — Tier5: clean public APIs, no login/key。

源:
  hn             Hacker News (Algolia)     https://hn.algolia.com/api/v1/search
  stackexchange  StackExchange 搜索        https://api.stackexchange.com/2.3/search/advanced
  reddit         arctic-shift 归档(Reddit 官方 API 反爬,归档是通行做法)。
                 注意:全文 query 必须配 --subreddits 限定,纯关键词会被拒。

实现要点:
  - urllib 换 requests:本机是 SOCKS 代理,urllib 不支持会 SSL EOF(journal.py 踩过)。
  - 输出到 intel 工作目录 ~/market-intel/out/en_<source>.jsonl,append + id 去重。
  - 保守限速 + 不盲目重试限流端点。

用法:
  python -m intel.collect_en --source hn --keywords "Polymarket" "prediction market" --pages 2
  python -m intel.collect_en --source reddit --keywords "GPT-6" --subreddits singularity OpenAI
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from intel.paths import OUT

UA = "polymarket-agent-intel/1.0 (small-volume research)"


def _get(url: str, params: dict | None = None, tries: int = 3, pause: float = 1.5):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 429:          # 限流:退避,不盲目重试
                time.sleep(pause * (i + 2) * 2)
                continue
            r.raise_for_status()
            return r.json(), None
        except Exception as e:
            if i == tries - 1:
                return None, str(e)[:120]
            time.sleep(pause * (i + 1))
    return None, "exhausted(可能限流)"


def _writer(source: str):
    path = os.path.join(OUT, f"en_{source}.jsonl")
    existing = set()
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                existing.add(json.loads(line).get("id"))
            except Exception:
                pass
    return open(path, "a", encoding="utf-8"), existing, path


def collect_hn(keywords: list[str], pages: int, pause: float) -> int:
    fh, existing, path = _writer("hn")
    new = 0
    for kw in keywords:
        for page in range(pages):
            data, err = _get("https://hn.algolia.com/api/v1/search",
                             {"query": kw, "page": page, "hitsPerPage": 50})
            if err or not data:
                print(f"  hn '{kw}' p{page}: {err}", file=sys.stderr)
                break
            for h in data.get("hits", []):
                hid = f"hn_{h.get('objectID')}"
                if hid in existing:
                    continue
                fh.write(json.dumps({
                    "id": hid, "kw": kw, "title": h.get("title") or h.get("story_title"),
                    "url": h.get("url"), "points": h.get("points"),
                    "num_comments": h.get("num_comments"), "created_at": h.get("created_at"),
                }, ensure_ascii=False) + "\n")
                existing.add(hid); new += 1
            time.sleep(pause)
    fh.close()
    print(f"hn: +{new} -> {path}")
    return new


def collect_stackexchange(keywords: list[str], sites: list[str], pause: float) -> int:
    fh, existing, path = _writer("stackexchange")
    new = 0
    for site in sites or ["stackoverflow"]:
        for kw in keywords:
            data, err = _get("https://api.stackexchange.com/2.3/search/advanced",
                             {"q": kw, "site": site, "pagesize": 50, "order": "desc", "sort": "relevance"})
            if err or not data:
                print(f"  se '{kw}'@{site}: {err}", file=sys.stderr)
                continue
            for q in data.get("items", []):
                qid = f"se_{site}_{q.get('question_id')}"
                if qid in existing:
                    continue
                fh.write(json.dumps({
                    "id": qid, "kw": kw, "site": site, "title": q.get("title"),
                    "link": q.get("link"), "score": q.get("score"),
                    "answers": q.get("answer_count"), "created": q.get("creation_date"),
                }, ensure_ascii=False) + "\n")
                existing.add(qid); new += 1
            time.sleep(pause)
            if data and data.get("quota_remaining", 999) < 20:
                print("  se: 配额将尽,停", file=sys.stderr)
                break
    fh.close()
    print(f"stackexchange: +{new} -> {path}")
    return new


def collect_reddit(keywords: list[str], subreddits: list[str], pause: float) -> int:
    if not subreddits:
        print("reddit: arctic-shift 全文检索必须配 --subreddits,跳过", file=sys.stderr)
        return 0
    fh, existing, path = _writer("reddit")
    new = 0
    for sub in subreddits:
        for kw in keywords:
            data, err = _get("https://arctic-shift.photon-reddit.com/api/posts/search",
                             {"subreddit": sub, "query": kw, "limit": 50})
            if err or not data:
                print(f"  reddit '{kw}'@r/{sub}: {err}", file=sys.stderr)
                continue
            for p in (data.get("data") or []):
                pid = f"rd_{p.get('id')}"
                if pid in existing:
                    continue
                fh.write(json.dumps({
                    "id": pid, "kw": kw, "sub": sub, "title": p.get("title"),
                    "score": p.get("score"), "num_comments": p.get("num_comments"),
                    "created_utc": p.get("created_utc"),
                    "permalink": p.get("permalink"),
                }, ensure_ascii=False) + "\n")
                existing.add(pid); new += 1
            time.sleep(pause)
    fh.close()
    print(f"reddit: +{new} -> {path}")
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description="EN 公开源采集(Tier5)")
    ap.add_argument("--source", required=True, choices=["hn", "stackexchange", "reddit"])
    ap.add_argument("--keywords", nargs="+", required=True)
    ap.add_argument("--pages", type=int, default=2, help="hn 每词页数")
    ap.add_argument("--sites", nargs="*", default=[], help="stackexchange 站点")
    ap.add_argument("--subreddits", nargs="*", default=[], help="reddit 必填")
    ap.add_argument("--pause", type=float, default=1.2)
    a = ap.parse_args()
    if a.source == "hn":
        collect_hn(a.keywords, a.pages, a.pause)
    elif a.source == "stackexchange":
        collect_stackexchange(a.keywords, a.sites, a.pause)
    else:
        collect_reddit(a.keywords, a.subreddits, a.pause)


if __name__ == "__main__":
    main()
