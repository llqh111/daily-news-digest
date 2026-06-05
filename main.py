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

# 每个 RSS 源最多取几条（抓多一点，留给后面筛选挑）
MAX_PER_FEED = 8
# 只保留多少小时内的新闻（晨报要新鲜，48h 给国际时差留余地）
TIME_WINDOW_HOURS = 48
# 第一层代码粗筛后，最多留多少条候选交给 AI 精选
CANDIDATE_POOL = 30

# RSS 新闻源（国际要闻 + 科技/AI + 财经市场）
RSS_FEEDS = [
    # ── 国际要闻 ──
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    {"name": "Guardian World", "url": "https://www.theguardian.com/world/rss", "category": "国际"},
    {"name": "NPR Top News", "url": "https://feeds.npr.org/1001/rss.xml", "category": "国际"},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "国际"},
    # ── 科技 / AI ──
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "科技"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "category": "科技"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "category": "科技"},
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/", "category": "科技"},
    # ── 财经市场 ──
    {"name": "CNBC Top News", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "category": "财经"},
    {"name": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "category": "财经"},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "财经"},
]

# ═══════════════════════════════════════════════════
#  重要性打分用的关键词表（想调"什么算重要新闻"，改这里）
# ═══════════════════════════════════════════════════
#
# 思路：标题里出现这些"信号词"就加分，分数越高越可能进晨报。
# 权重越大代表越重要。全部用小写，匹配时不区分大小写。
#
# 高权重（+3）：重大地缘 / 货币政策 / 头部科技公司动作
HIGH_SIGNAL_KEYWORDS = [
    # 地缘与重大事件
    "war", "ceasefire", "invasion", "missile", "nuclear", "sanction",
    "coup", "election", "summit", "crisis", "attack", "strike",
    # 货币政策与宏观
    "fed", "rate cut", "rate hike", "inflation", "recession", "tariff",
    "central bank", "gdp", "default", "stimulus",
    # 头部科技 / AI
    "openai", "nvidia", "anthropic", "deepseek", "gpt", "chip ban",
    "semiconductor", "breakthrough",
]
# 中权重（+1）：常规但有价值的商业 / 科技 / 市场新闻
MEDIUM_SIGNAL_KEYWORDS = [
    "ai", "apple", "google", "microsoft", "amazon", "tesla", "meta",
    "earnings", "ipo", "merger", "acquisition", "launch", "stocks",
    "market", "oil", "gold", "bitcoin", "lawsuit", "deal", "ban",
]
# 负权重（-2）：标题命中就降权（多半是软新闻 / 娱乐 / 凑数）
LOW_VALUE_KEYWORDS = [
    "recipe", "celebrity", "gossip", "royal", "horoscope", "fashion",
    "recap", "quiz", "best deals", "how to watch", "trailer",
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


def score_importance(title: str, summary: str, published: datetime | None) -> float:
    """
    给一条新闻打"重要性分数"——分数越高，越优先进晨报。

    这是整个项目里最体现【你的判断】的地方：什么算重要，没有标准答案。
    下面是一套能直接跑的默认规则，你可以随时调：
      · 改上面三个关键词表（HIGH_SIGNAL / MEDIUM_SIGNAL / LOW_VALUE）
      · 改下面各项的权重数字
      · TODO（给你练手）：想加新规则就往这里加，比如
          - 标题带问号的多半是标题党，可以 score -= 1
          - 来源是某个你特别信任的媒体，可以额外 +2
          - summary 里出现某些词也加分（现在只看 title）

    返回一个浮点分数，fetch 阶段会按它从高到低排序。
    """
    text = f"{title} {summary}".lower()
    score = 0.0

    # ① 关键词信号：命中高/中/负权重词就加减分
    for kw in HIGH_SIGNAL_KEYWORDS:
        if kw in text:
            score += 3
    for kw in MEDIUM_SIGNAL_KEYWORDS:
        if kw in text:
            score += 1
    for kw in LOW_VALUE_KEYWORDS:
        if kw in text:
            score -= 2

    # ② 新鲜度加成：越新的越加分（晨报要"及时"）
    if published is not None:
        hours_old = (datetime.now(timezone.utc) - published).total_seconds() / 3600
        if hours_old <= 6:
            score += 3      # 6 小时内：热乎的
        elif hours_old <= 12:
            score += 2
        elif hours_old <= 24:
            score += 1
        # 超过 24h 不额外加分（但只要在时间窗口内仍保留）

    # TODO（你来加）：在这里写你自己的评分规则 ↓↓↓


    return score


def fetch_all_feeds() -> list[dict]:
    """抓取所有 RSS 源 → 时间窗口过滤 → 重要性打分 → 取分数最高的一批候选"""
    now_utc = datetime.now(timezone.utc)
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

            published = parse_published(entry)

            # 时间窗口过滤：太旧的直接丢（有发布时间才判断；没时间的保留）
            if published is not None:
                hours_old = (now_utc - published).total_seconds() / 3600
                if hours_old > TIME_WINDOW_HOURS:
                    continue

            SEEN_TITLES.add(title_lower)
            if link:
                SEEN_LINKS.add(link)

            articles.append({
                "title": title,
                "summary": summary[:500],  # 留更多细节给 AI（原来只留 300）
                "link": link,
                "source": feed_info["name"],
                "category": feed_info["category"],
                "published": published,
                "score": score_importance(title, summary, published),
            })
            count += 1

        log.info(f"  {feed_info['name']}: 抓到 {count} 条")

    # 第一层粗筛：按重要性分数从高到低排序，取分数最高的一批候选
    articles.sort(key=lambda a: a["score"], reverse=True)
    top = articles[:CANDIDATE_POOL]
    log.info(f"粗筛后保留 {len(top)} 条候选（共抓到 {len(articles)} 条）")
    if top:
        log.info(f"  分数区间：{top[0]['score']:.0f} ~ {top[-1]['score']:.0f}")
    return top


def summarize_with_deepseek(articles: list[dict]) -> str:
    """把新闻列表发给 DeepSeek，让它用中文总结成每日简报"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("❌ 没有设置 DEEPSEEK_API_KEY，请检查 .env 文件")

    # 构造给 AI 看的新闻列表（带来源、分类、链接，供它判断和精选）
    articles_text_parts = []
    for i, art in enumerate(articles, 1):
        parts = [f"{i}. [{art['category']}] {art['title']}"]
        if art["summary"]:
            parts.append(f"   摘要: {art['summary']}")
        parts.append(f"   来源: {art['source']}")
        if art["link"]:
            parts.append(f"   链接: {art['link']}")
        articles_text_parts.append("\n".join(parts))
    articles_text = "\n\n".join(articles_text_parts)

    # 晨报体提示词：专业编辑·有观点，写出"看晨报"的感觉
    system_prompt = (
        "你是一位资深的中文财经科技新闻主编，每天为高知读者撰写一份深度『晨报』。\n"
        "你的风格像《财新》《FT中文网》：冷静专业，但敢下判断、点出影响与看点，不做无观点的复述。\n"
        "\n"
        "【任务】我会给你一批已经过初筛的候选新闻（共约 30 条，已按重要性排过序）。\n"
        "请你二次精选，从中挑出最重要、最有信息量的 15-20 条，编成一份结构化晨报。\n"
        "不重要、重复、过于琐碎或纯软文的，果断舍弃，不要硬凑数量。\n"
        "\n"
        "【输出结构】用 Markdown：\n"
        "1. 顶部一段『今日导语』（3-4 句）：概括今天全球最值得关注的主线与基调。\n"
        "2. 分三组（哪组没料就省略该组）：\n"
        "   ## 🌍 国际要闻\n"
        "   ## 💻 科技与 AI\n"
        "   ## 💰 财经市场\n"
        "3. 每条新闻按这个格式写：\n"
        "   **加粗的中文标题**\n"
        "   2-4 句正文：交代背景、关键细节、和它意味着什么（来龙去脉，别只翻译原标题）。\n"
        "   > 【点评】一句你作为主编的判断——影响、看点、或值得警惕之处。\n"
        "4. 结尾写一段『编辑手记 / 今日看点』（3-5 句）：串联今天的脉络，给出前瞻或提醒。\n"
        "\n"
        "【要求】\n"
        "- 全程中文，专有名词首次出现可附英文原名。\n"
        "- 点评要有信息增量和观点，不要写『值得关注』这种空话。\n"
        "- 财经部分尽量点到当日市场情绪/资金面/政策含义。\n"
        "- 不要编造候选清单里没有的事实；信息不足时宁可少写。"
    )

    today_str = datetime.now(TZ).strftime("%Y年%m月%d日 %A")
    user_prompt = (
        f"今天是 {today_str}。以下是已初筛的候选新闻（共 {len(articles)} 条），"
        f"请按要求精选并编成今天的深度晨报：\n\n{articles_text}"
    )

    log.info(f"发送 {len(articles)} 条候选到 DeepSeek 进行精选与撰写...")

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
            "max_tokens": 4500,
        },
        timeout=120,
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
