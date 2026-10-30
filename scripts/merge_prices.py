#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_prices.py — 把 Issue #1 里所有玩家贡献的 AUX 价格数据合并成总表。

数据源：GitHub Issue #1 的 body + 所有评论，每条评论里贴一个 ```json 围栏。
输出：data/auction_prices.json（合并后的总表，程序启动时拉取）。

过滤规则（防假数据）：
  1. scan_time 超过 MAX_AGE_DAYS 天的丢弃
  2. 同一 itemID 贡献者不足 MIN_CONTRIBUTORS 个的丢弃
  3. 与中位数偏差超过 OUTLIER_FACTOR 倍的离谱价丢弃
  4. 同一 contributor 对同一 itemID 多次贡献，只取最新一条

本地调试：python scripts/merge_prices.py --local
正式跑：  在 GitHub Actions 里，环境变量 GITHUB_TOKEN 自动注入。
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ===== 可调参数 =====
REPO = "zdudao/laoxu"
ISSUE_NUMBER = 1
MAX_AGE_DAYS = 7          # 只信最近 7 天的数据
MIN_CONTRIBUTORS = 3      # 至少 3 个不同玩家贡献过
OUTLIER_FACTOR = 3.0      # 与中位数偏差超过 3 倍视为离谱价
OUTPUT_PATH = os.path.join("data", "auction_prices.json")

API = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"


def http_get(url, token=None):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "shelllab-merge-bot")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_issue_and_comments(token=None):
    """拉 Issue body + 所有评论（处理分页）。"""
    issue = http_get(API, token)
    comments = []
    comments_url = issue.get("comments_url", API + "/comments")
    page = 1
    while True:
        url = f"{comments_url}?per_page=100&page={page}"
        batch = http_get(url, token)
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return issue, comments


JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_blocks(text):
    """从 markdown 文本里抽 ```json ... ``` 围栏。"""
    blocks = []
    for m in JSON_BLOCK_RE.finditer(text or ""):
        try:
            blocks.append(json.loads(m.group(1)))
        except json.JSONDecodeError as e:
            print(f"  ! 跳过一段 JSON（解析失败）: {e}", file=sys.stderr)
    return blocks


def collect_contributions(issue, comments):
    """
    返回 dict: itemID(str) -> list of {contributor, buyout, scan_time}
    """
    contributions = {}
    # Issue body 本身也算一条贡献
    sources = [("issue_body", issue.get("body", ""))]
    for c in comments:
        login = (c.get("user") or {}).get("login", "unknown")
        sources.append((login, c.get("body", "")))

    for login, text in sources:
        for block in extract_json_blocks(text):
            items = block.get("items", {})
            if not isinstance(items, dict):
                continue
            for item_id, rec in items.items():
                try:
                    buyout = int(rec.get("buyout", 0))
                    scan_time = int(rec.get("scan_time", 0))
                except (TypeError, ValueError):
                    continue
                if buyout <= 0 or scan_time <= 0:
                    continue
                contributions.setdefault(str(item_id), []).append({
                    "contributor": login,
                    "buyout": buyout,
                    "scan_time": scan_time,
                })
    return contributions


def merge(contributions):
    """按 itemID 过滤 + 合并。"""
    now = int(time.time())
    max_age = MAX_AGE_DAYS * 86400
    result = {}

    for item_id, records in contributions.items():
        # 规则 1：只保留最近 7 天
        fresh = [r for r in records if now - r["scan_time"] <= max_age]
        if not fresh:
            continue

        # 规则 2：同一 contributor 去重，只取最新
        by_user = {}
        for r in fresh:
            u = r["contributor"]
            if u not in by_user or r["scan_time"] > by_user[u]["scan_time"]:
                by_user[u] = r
        fresh = list(by_user.values())

        # 规则 3：贡献者数不足
        if len(fresh) < MIN_CONTRIBUTORS:
            continue

        # 规则 4：离谱价剔除（中位数 ± factor 倍之外的丢）
        prices = sorted(r["buyout"] for r in fresh)
        median = statistics.median(prices)
        if median <= 0:
            continue
        sane = [r for r in fresh
                if median / OUTLIER_FACTOR <= r["buyout"] <= median * OUTLIER_FACTOR]
        if len(sane) < MIN_CONTRIBUTORS:
            continue

        # 取最新 scan_time 的那条
        best = max(sane, key=lambda r: r["scan_time"])
        result[item_id] = {
            "buyout": best["buyout"],
            "updated_at": best["scan_time"],
            "votes": len(sane),
        }

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true",
                    help="本地调试：从 stdin 读 JSON（模拟 issue+comments）")
    args = ap.parse_args()

    if args.local:
        # 本地调试模式：从 stdin 读模拟数据
        print("[local] 从 stdin 读取模拟 issue+comments JSON ...")
        data = json.load(sys.stdin)
        issue = {"body": data.get("body", "")}
        comments = data.get("comments", [])
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            print("错误：未设置 GITHUB_TOKEN 环境变量", file=sys.stderr)
            sys.exit(1)
        print(f"[fetch] 拉取 Issue #{ISSUE_NUMBER} ...")
        issue, comments = fetch_issue_and_comments(token)
        print(f"  body 长度={len(issue.get('body',''))}, 评论数={len(comments)}")

    contributions = collect_contributions(issue, comments)
    print(f"[collect] 收到 {len(contributions)} 个物品的原始贡献")

    merged = merge(contributions)
    print(f"[merge] 过滤后保留 {len(merged)} 个物品")

    if not merged:
        print("[skip] 没有可写入的数据，不生成文件（避免清空已有总表）")
        return

    # 统计贡献者总数
    all_contributors = set()
    for recs in contributions.values():
        for r in recs:
            all_contributors.add(r["contributor"])

    output = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contributors_count": len(all_contributors),
        "items_count": len(merged),
        "items": merged,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[write] 已写入 {OUTPUT_PATH}（{len(merged)} 条价格）")


if __name__ == "__main__":
    main()
