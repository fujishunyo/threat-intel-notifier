#!/usr/bin/env python3
"""
RSSフィードを巡回し、新着記事をGemini APIで日本語要約した上でDiscordのWebhookへ通知するスクリプト。
GitHub Actions等で定期実行することを想定。

必要なパッケージ: feedparser, requests, beautifulsoup4
    pip install feedparser requests beautifulsoup4
"""

import html
import json
import os
import re
import sys
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup


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


def fetch_article_text(url: str, max_chars: int = 8000) -> str:
    """記事ページ本文をできるだけプレーンテキストで取得する(失敗したら空文字を返す)。"""
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RSSDigestBot/1.0)"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[WARN] 記事本文の取得に失敗: {url} ({e})", file=sys.stderr)
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # script/style/nav/footerなど本文に不要な要素を除去
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    # 本文らしき領域を優先的に狙う(見つからなければbody全体)
    main = soup.find("article") or soup.find("main") or soup.body or soup

    text = main.get_text(separator="\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()

    return text[:max_chars]


def summarize_with_gemini(title: str, article_text: str, fallback_summary: str) -> str:
    """Gemini APIで記事を日本語要約する。APIキー未設定・失敗時はRSSの抜粋にフォールバック。"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return fallback_summary

    source_text = article_text if article_text else fallback_summary
    if not source_text:
        return ""

    prompt = f"""以下はセキュリティ脅威分析ブログの記事です。日本語で要約してください。

タイトル: {title}

本文:
{source_text}

要約のルール:
- 400字程度で簡潔に
- 「何の脅威/マルウェアの話か」「攻撃手法・技術的なポイント(難読化、サンドボックス回避、C2手法など、あれば特に)」「読者が注意すべき点」を含める
- 前置きなしで要約本文だけを出力する"""

    try:
        resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return fallback_summary
        parts = candidates[0].get("content", {}).get("parts", [])
        summary = "".join(p.get("text", "") for p in parts).strip()
        return summary or fallback_summary
    except requests.RequestException as e:
        print(f"[WARN] Gemini要約に失敗、RSS抜粋を使用します: {e}", file=sys.stderr)
        return fallback_summary

# ---- 設定: 監視したいRSSフィードをここに追加 ----
# 各ブログのトップページのソースから <link rel="alternate" type="application/rss+xml">
# を探して、正しいフィードURLに置き換えてください。
FEEDS = {
    "Unit 42": "https://unit42.paloaltonetworks.com/feed/",
    "Securelist (Kaspersky)": "https://securelist.com/feed/",
    "Cisco Talos": "https://blog.talosintelligence.com/rss/",
    "Microsoft Threat Intelligence": "https://www.microsoft.com/en-us/security/blog/topic/threat-intelligence/feed/",
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


def post_to_discord(feed_name: str, title: str, link: str, japanese_summary: str) -> None:
    # Discordの埋め込み(Embed)として見やすく整形
    embed = {
        "title": title[:250],
        "url": link,
        "description": (japanese_summary or "")[:1500],
        "author": {"name": feed_name},
        "color": 0xE74C3C,
        "footer": {"text": "要約 by Gemini"},
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
            fallback_summary = clean_summary(entry.get("summary", ""))

            print(f"[NEW] {feed_name}: {title}")

            article_text = fetch_article_text(link)
            japanese_summary = summarize_with_gemini(title, article_text, fallback_summary)

            post_to_discord(feed_name, title, link, japanese_summary)

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
