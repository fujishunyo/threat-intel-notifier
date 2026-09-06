#!/usr/bin/env python3
"""
RSSフィードを巡回し、未通知の新着記事があればDiscordのWebhookへ投稿するスクリプト。
GitHub Actions等で定期実行することを想定。

必要なパッケージ: feedparser, requests
    pip install feedparser requests
"""

import html
import json
import os
import re
import sys
from pathlib import Path

import feedparser
import requests


def clean_summary(raw_html: str) -> str:
    """RSSのsummaryに含まれるHTMLタグを除去し、読みやすいプレーンテキストにする。"""
    if not raw_html:
        return ""

    # <br>, </p> などブロック要素は改行に変換してから、残りのタグを除去
    text = re.sub(r"<(br|/p|/div)\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)

    # &amp; や &#8217; などのHTMLエンティティをデコード
    text = html.unescape(text)

    # 記事末尾によく付く "The post ... appeared first on ..." の定型文を除去
    text = re.sub(r"\s*The post .* appeared first on .*\.?\s*$", "", text, flags=re.IGNORECASE | re.DOTALL)

    # 連続する空白・改行を整理
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    return text.strip()

# ---- 設定: 監視したいRSSフィードをここに追加 ----
# 各ブログのトップページのソースから <link rel="alternate" type="application/rss+xml">
# を探して、正しいフィードURLに置き換えてください。
FEEDS = {
    "Unit 42": "https://unit42.paloaltonetworks.com/feed/",
    "Securelist (Kaspersky)": "https://securelist.com/feed/",
    "Trellix": "https://www.trellix.com/blogs/feed/",  # 要確認・要修正
}

# Discord Webhook URL は環境変数から取得(コードに直書きしない)
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# 既読記事のIDを保存するファイル(リポジトリ内でコミットして永続化する)
SEEN_FILE = Path(__file__).parent / "seen_ids.json"

# 一度に大量投稿してレート制限にかからないよう、フィードごとの最大通知数
MAX_ITEMS_PER_FEED = 5


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def post_to_discord(feed_name: str, title: str, link: str, summary: str) -> None:
    # Discordの埋め込み(Embed)として見やすく整形
    embed = {
        "title": title[:250],
        "url": link,
        "description": (summary or "")[:500],
        "author": {"name": feed_name},
        "color": 0xE74C3C,
    }
    payload = {"embeds": [embed]}

    resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code >= 300:
        print(f"[WARN] Discord post failed ({resp.status_code}): {resp.text}", file=sys.stderr)


def main() -> None:
    if not WEBHOOK_URL:
        print("[ERROR] 環境変数 DISCORD_WEBHOOK_URL が設定されていません。", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    updated = False

    for feed_name, feed_url in FEEDS.items():
        parsed = feedparser.parse(feed_url)

        if parsed.bozo and not parsed.entries:
            print(f"[WARN] '{feed_name}' のフィード取得に失敗した可能性があります: {feed_url}")
            continue

        seen_ids = set(seen.get(feed_name, []))
        new_ids = []

        # 新しい順に並んでいる前提で、最大件数だけ処理
        for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen_ids:
                continue

            title = entry.get("title", "(no title)")
            link = entry.get("link", feed_url)
            summary = clean_summary(entry.get("summary", ""))

            print(f"[NEW] {feed_name}: {title}")
            post_to_discord(feed_name, title, link, summary)

            new_ids.append(entry_id)
            updated = True

        if new_ids:
            seen_ids.update(new_ids)
            seen[feed_name] = list(seen_ids)

    if updated:
        save_seen(seen)
        print("既読リストを更新しました。")
    else:
        print("新着記事はありませんでした。")


if __name__ == "__main__":
    main()
