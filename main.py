"""
每日全球要闻推送
──────────────────────────────────
每天自动抓取国际新闻 RSS → DeepSeek AI 总结为中文 → Server酱 推送到微信

在 GitHub Actions 上定时运行，无需本地服务器。
"""

import os
import time
import hashlib
import logging
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from dotenv import load_dotenv

# ── 加载 .env 文件中的密钥 ──────────────────────────
load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY")

# ── 日志 ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  配置区（想改新闻源、推送时间，改这里就行）
# ═══════════════════════════════════════════════════

# 北京时间
TZ = timezone(timedelta(hours=8))

# 每个 RSS 源最多取几条
MAX_PER_FEED = 5
# 总共取多少条送给 AI
MAX_TOTAL = 20

# RSS 新闻源（国际要闻 + 财经市场）
RSS_FEEDS = [
    # ── 国际要闻 ──
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    {"name": "Guardian World", "url": "https://www.theguardian.com/world/rss", "category": "国际"},
    {"name": "NPR Top News", "url": "https://feeds.npr.org/1001/rss.xml", "category": "国际"},
    {"name": "NHK World", "url": "https://www3.nhk.or.jp/nhkworld/en/news/rss.xml", "category": "国际"},
    # ── 财经市场 ──
    {"name": "CNBC Top News", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "category": "财经"},
    {"name": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "category": "财经"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "财经"},
]

# 去重用：记录已抓到的标题和链接
SEEN_TITLES = set()
SEEN_LINKS = set()


def parse_published(entry) -> datetime | None:
    """把 RSS 里各种格式的时间字符串统一转成 datetime"""
    raw = entry.get("published", "") or entry.get("updated", "")
    if not raw:
        return None
    # feedparser 有时已经帮你解析好了
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        try:
            return datetime(*struct[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def clean_html(text: str) -> str:
    """去掉 HTML 标签，只留纯文本"""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return text.strip()


def fetch_all_feeds() -> list[dict]:
    """抓取所有 RSS 源，去重、限数，返回文章列表"""
    today = datetime.now(TZ).date()
    articles: list[dict] = []

    for feed_info in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
        except Exception as e:
            log.warning(f"解析失败 {feed_info['name']}: {e}")
            continue

        count = 0
        for entry in feed.entries:
            if count >= MAX_PER_FEED:
                break

            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = clean_html(entry.get("summary", entry.get("description", "")))

            if not title:
                continue

            # 去重
            title_lower = title.lower()
            if title_lower in SEEN_TITLES or (link and link in SEEN_LINKS):
                continue

            # 简单过滤：跳过视频/体育/娱乐类标题
            skip_keywords = ["sports", "sport:", "celebrity", "video:", "watch:", "live stream"]
            if any(title_lower.startswith(k) for k in skip_keywords):
                continue

            SEEN_TITLES.add(title_lower)
            if link:
                SEEN_LINKS.add(link)

            articles.append({
                "title": title,
                "summary": summary[:300],  # 截断太长的摘要
                "link": link,
                "source": feed_info["name"],
                "category": feed_info["category"],
                "published": parse_published(entry),
            })
            count += 1

        log.info(f"  {feed_info['name']}: 抓到 {count} 条")

    # 按发布时间倒序，最新的在前
    articles.sort(
        key=lambda a: a["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return articles[:MAX_TOTAL]


def summarize_with_deepseek(articles: list[dict]) -> str:
    """把新闻列表发给 DeepSeek，让它用中文总结成每日简报"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("❌ 没有设置 DEEPSEEK_API_KEY，请检查 .env 文件")

    # 构造给 AI 看的新闻列表
    articles_text_parts = []
    for i, art in enumerate(articles, 1):
        parts = [f"{i}. [{art['category']}] {art['title']}"]
        if art["summary"]:
            parts.append(f"   摘要: {art['summary']}")
        parts.append(f"   来源: {art['source']}")
        articles_text_parts.append("\n".join(parts))
    articles_text = "\n\n".join(articles_text_parts)

    # 中文总结的提示词
    system_prompt = (
        "你是一个专业的新闻编辑，负责为中文读者编写每日全球要闻简报。\n"
        "要求：\n"
        "1. 用中文撰写，语气专业但不枯燥\n"
        "2. 每条新闻用一行概括，包含标题和 1-2 句要点\n"
        "3. 按「🌍 国际要闻」和「💰 财经市场」分组\n"
        "4. 每组选最重要的 5-8 条，不要逐条罗列\n"
        "5. 开头写「📰 每日全球要闻 — {日期}」\n"
        "6. 结尾可以加一句简短的市场点评（如果今天有财经新闻的话）\n"
        "7. 使用 Markdown 格式，用 **粗体** 标出新闻标题"
    )

    today_str = datetime.now(TZ).strftime("%Y年%m月%d日")
    user_prompt = f"今天是 {today_str}。以下是今天抓取到的新闻列表，请按要求生成中文简报：\n\n{articles_text}"

    log.info(f"发送 {len(articles)} 条新闻到 DeepSeek 进行总结...")

    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 3000,
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()

    content = data["choices"][0]["message"]["content"]
    tokens_used = data.get("usage", {}).get("total_tokens", "?")
    log.info(f"DeepSeek 返回 {len(content)} 字，消耗 {tokens_used} tokens")
    return content


def push_to_wechat(content: str):
    """通过 Server酱 把内容推送到微信"""
    if not SERVERCHAN_SENDKEY:
        raise RuntimeError("❌ 没有设置 SERVERCHAN_SENDKEY，请检查 .env 文件")

    today_str = datetime.now(TZ).strftime("%m/%d")

    log.info("正在推送到微信 (Server酱)...")
    resp = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send",
        data={
            "title": f"📰 每日全球要闻 — {today_str}",
            "desp": content,
        },
        timeout=30,
    )
    result = resp.json()
    if result.get("code") == 0:
        log.info("✅ 推送成功！请查看微信")
    else:
        log.error(f"❌ 推送失败: {result}")
        raise RuntimeError(f"Server酱返回错误: {result}")


# ═══════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════

def main():
    log.info("=" * 50)
    log.info("📡 开始抓取 RSS 新闻...")
    articles = fetch_all_feeds()
    log.info(f"共抓到 {len(articles)} 条有效新闻")

    if not articles:
        log.error("没有抓到任何新闻，退出")
        return

    log.info("🤖 调用 DeepSeek 生成中文简报...")
    summary = summarize_with_deepseek(articles)

    log.info("📲 推送到微信...")
    push_to_wechat(summary)

    log.info("=" * 50)
    log.info("🎉 全部完成！")

    # 打印摘要到日志（方便在 GitHub Actions 里查看）
    print("\n" + "=" * 50)
    print(summary)
    print("=" * 50)


if __name__ == "__main__":
    main()
