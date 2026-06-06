"""
每日全球要闻推送
──────────────────────────────────
每天自动抓取国际新闻 RSS → DeepSeek AI 总结为中文 → Server酱 推送到微信

在 GitHub Actions 上定时运行，无需本地服务器。
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
import trafilatura  # 从新闻原网页里提取正文（去广告/导航，只留文章本体）
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
TIME_WINDOW_HOURS = 24
# 聚类去重 + 打分后，留多少条「代表作」去抓正文全文交给 AI。
# AI 会从这批里再精选 15-20 条，所以这个数要比最终条数大一些，给 AI 留挑选余地。
CANDIDATE_POOL = 24
# 抓正文时的并发数和单条超时（秒）。并发快但别太猛，免得被网站当攻击。
FULLTEXT_WORKERS = 6
FULLTEXT_TIMEOUT = 12
# 喂给 AI 的正文最多保留多少字（控制 token 成本，2500 字够写出深度了）
FULLTEXT_MAX_CHARS = 2500

# RSS 新闻源（国际要闻 + 科技/AI + 财经市场）
#
# 关于 "reference": True ──────────────────────────────
# 路透/AP/彭博这些顶级通讯社关闭了公开 RSS，只能走 Google News 代理，
# 而 Google News 的链接是加密跳转、抓不到正文全文。所以把它们标成「参考源」：
#   · 它们的报道只用来给新闻「投重要性票」（触发多源印证加分）
#   · 同一件事若也被能抓全文的源报道，代表作优先用那条深度源
#   · 只有某事仅被参考源报道时，才用它本身（浅，但重要的事不漏）
# 没标 reference 的都是直连真 RSS，能抓全文、有完整深度。
RSS_FEEDS = [
    # ── 国际要闻 ──
    {"name": "Reuters", "url": "https://news.google.com/rss/search?q=when:1d+site:reuters.com&hl=en-US&gl=US&ceid=US:en", "category": "国际", "reference": True},
    {"name": "AP", "url": "https://news.google.com/rss/search?q=when:1d+site:apnews.com&hl=en-US&gl=US&ceid=US:en", "category": "国际", "reference": True},
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    {"name": "DW", "url": "https://rss.dw.com/xml/rss-en-world", "category": "国际"},
    {"name": "Nikkei Asia", "url": "https://asia.nikkei.com/rss/feed/nar", "category": "国际"},
    # ── 科技 / AI ──
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/", "category": "科技"},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage?points=100", "category": "科技"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "category": "科技"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "科技"},
    # ── 财经市场 ──
    {"name": "FT", "url": "https://www.ft.com/world?format=rss", "category": "财经"},
    {"name": "Reuters Business", "url": "https://news.google.com/rss/search?q=when:1d+site:reuters.com+(markets+OR+economy+OR+stocks+OR+earnings)&hl=en-US&gl=US&ceid=US:en", "category": "财经", "reference": True},
    {"name": "CNBC", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "category": "财经"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "财经"},
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

# ═══════════════════════════════════════════════════
#  来源可信度分级（想调「更信哪家媒体」，改这里的数字）
# ═══════════════════════════════════════════════════
#
# 思路：硬新闻、调查能力强的源加分；聚合/快讯类给 0 或降权。
# 这是「主编的口味」，没有标准答案——下面是一套默认值，你随时改数字即可。
# key 要和上面 RSS_FEEDS 里的 name 完全一致；没列到的源默认 0 分。
SOURCE_TRUST = {
    # ── 国际：一线通讯社 / 硬新闻（+2），日经亚洲（+1）──
    "Reuters": 2,
    "AP": 2,
    "BBC World": 2,
    "DW": 2,
    "Nikkei Asia": 1,
    # ── 科技：MIT/HN 顶级（+2），Ars/Verge 专业（+1）──
    "MIT Tech Review": 2,
    "Hacker News": 2,
    "Ars Technica": 1,
    "The Verge": 1,
    # ── 财经：FT/路透财经（+2），CNBC/CoinDesk（+1）──
    "FT": 2,
    "Reuters Business": 2,
    "CNBC": 1,
    "CoinDesk": 1,
}

# 标题党 / 软文句式：标题命中其一就降权（-2）。用正则匹配。
CLICKBAIT_PATTERNS = [
    r"\bhere'?s why\b",          # "Here's why ..."
    r"\bhere'?s what\b",         # "Here's what ..."
    r"^\d+\s+(things|ways|reasons|signs)\b",   # "7 things you should..."
    r"\byou (won'?t|wont) believe\b",
    r"\bthis is (why|how|what)\b",
    r"\bwhat to know\b",
    r"\bwe (tried|tested|ranked)\b",
    r"\?\s*$",                   # 整条标题以问号结尾，多半是钓鱼式标题党
]

# 去重用：记录已抓到的标题和链接
SEEN_TITLES = set()
SEEN_LINKS = set()

# 已推送记录文件（避免早晚报重复推送同一条新闻）
SENT_LOG_FILE = os.path.join(os.path.dirname(__file__), "sent_articles.json")

# 标题聚类时要忽略的高频虚词（这些词到处都是，不能用来判断「是否同一件事」）
TITLE_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "at", "by",
    "with", "from", "as", "is", "are", "was", "were", "be", "been", "will",
    "says", "after", "over", "amid", "into", "out", "up", "new", "us", "uk",
    "report", "live", "news", "video", "watch", "update", "latest",
}


def load_sent_links() -> set[str]:
    """加载上次推送过的文章链接，用来跳过避免早晚报重复。"""
    if not os.path.exists(SENT_LOG_FILE):
        return set()
    try:
        with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        links = set(data.get("links", []))
        ts = data.get("ts", "unknown")
        log.info(f"已加载上次推送记录：{len(links)} 条（{ts}）")
        return links
    except Exception as e:
        log.warning(f"加载推送记录失败，将按无历史处理: {e}")
        return set()


def save_sent_links(links: list[str]) -> None:
    """保存本次候选文章链接，供下次运行去重用。"""
    data = {
        "ts": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "links": links,
    }
    try:
        with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info(f"已保存 {len(links)} 条推送记录到 {SENT_LOG_FILE}")
    except Exception as e:
        log.warning(f"保存推送记录失败（不影响推送）: {e}")


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
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return text.strip()


def score_importance(title: str, summary: str, published: datetime | None,
                     source: str = "") -> float:
    """
    给一条新闻打"重要性分数"——分数越高，越优先进晨报。

    这是整个项目里最体现【你的判断】的地方：什么算重要，没有标准答案。
    下面是一套能直接跑的默认规则，你可以随时调：
      · 改三个关键词表（HIGH_SIGNAL / MEDIUM_SIGNAL / LOW_VALUE）
      · 改 SOURCE_TRUST 里各家媒体的信任分
      · 改下面各项的权重数字

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

    # ③ 来源可信度：信得过的硬新闻源天然加分，聚合/快讯不加
    score += SOURCE_TRUST.get(source, 0)

    # ④ 标题党 / 软文惩罚：命中钓鱼式句式就降权（只看标题，别误伤正文）
    title_lower = title.lower()
    for pat in CLICKBAIT_PATTERNS:
        if re.search(pat, title_lower):
            score -= 2
            break   # 命中一个就够了，不重复扣

    return score


def title_keywords(title: str) -> set[str]:
    """把标题拆成「有信息量的关键词集合」，用来判断两条是不是同一件事。
    做法：转小写 → 只留字母数字 → 去掉太短的词和虚词。"""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in TITLE_STOPWORDS}


def same_story(words_a: set[str], words_b: set[str]) -> bool:
    """两条标题是否在讲同一件事。
    规则：共享的关键词 ≥3 个，或者「共享数 ≥2 且占了较短标题的一半以上」。
    这样既能抓住明显同题，又不会把只沾一个共同词的硬凑在一起。"""
    shared = words_a & words_b
    if len(shared) >= 3:
        return True
    smaller = min(len(words_a), len(words_b)) or 1
    return len(shared) >= 2 and len(shared) / smaller >= 0.5


def cluster_and_boost(articles: list[dict]) -> list[dict]:
    """把讲同一件事的新闻聚成一簇，每簇选一条「代表作」写进晨报，
    并按「这件事被几家媒体报道」给它加分——被广泛报道本身就是重要性信号。

    代表作的挑选体现「参考源只作参考」：
      · 优先选「非参考源」（能抓全文、有深度）当代表，哪怕它分数略低；
      · 重要性分取全簇最高分（保留路透/AP 等权威头条投的「重要票」）；
      · 只有整簇都是参考源时，才用参考源本身（浅，但重要的事不漏）。

    返回去重后的代表作列表（已重新按调整后分数排序）。
    """
    # 先按原始分数从高到低，方便后面在候选里挑最高分
    articles = sorted(articles, key=lambda a: a["score"], reverse=True)

    clusters: list[dict] = []   # 每个元素：{"members": [文章...], "words": 关键词集}
    for art in articles:
        words = title_keywords(art["title"])
        placed = False
        for c in clusters:
            if same_story(words, c["words"]):
                c["members"].append(art)
                c["words"] |= words   # 关键词并集，让后续判断更"见多识广"
                placed = True
                break
        if not placed:
            clusters.append({"members": [art], "words": words})

    reps: list[dict] = []
    for c in clusters:
        members = c["members"]
        size = len(members)

        # 代表作：优先在「非参考源」里挑分最高的；没有非参考源才退回全部
        non_ref = [m for m in members if not m.get("reference")]
        rep = max(non_ref or members, key=lambda m: m["score"])

        # 重要性 = 全簇最高分（哪怕来自参考源的权威报道）+ 多源印证加成
        best_score = max(m["score"] for m in members)
        extra_sources = size - 1
        rep["cluster_size"] = size
        rep["score"] = best_score + min(extra_sources * 1.5, 3)
        reps.append(rep)

    reps.sort(key=lambda a: a["score"], reverse=True)
    return reps


def fetch_all_feeds(skip_links: set[str] | None = None) -> list[dict]:
    """抓取所有 RSS 源 → 时间窗口过滤 → 打分 → 同题聚类去重 → 取分数最高的一批候选"""
    if skip_links is None:
        skip_links = set()
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

            # Google News 代理的标题带「 - 媒体名」后缀，去掉它，免得干扰聚类判断
            if feed_info.get("reference"):
                title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()

            # 去重
            title_lower = title.lower()
            if title_lower in SEEN_TITLES or (link and link in SEEN_LINKS):
                continue

            # 跳过上次推送过的文章（避免早晚报重复）
            if link and link in skip_links:
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
                "reference": feed_info.get("reference", False),
                "published": published,
                "score": score_importance(title, summary, published, feed_info["name"]),
            })
            count += 1

        log.info(f"  {feed_info['name']}: 抓到 {count} 条")

    # 同题聚类：把多家媒体报道的同一事件合并，按「被报道家数」加分，每簇只留代表作
    reps = cluster_and_boost(articles)
    merged = len(articles) - len(reps)
    log.info(f"同题聚类：{len(articles)} 条 → {len(reps)} 条（合并掉 {merged} 条重复报道）")

    # 取分数最高的一批代表作，作为交给 AI 的候选（后面会去抓正文全文）
    top = reps[:CANDIDATE_POOL]
    log.info(f"粗筛后保留 {len(top)} 条候选")
    if top:
        log.info(f"  分数区间：{top[0]['score']:.1f} ~ {top[-1]['score']:.1f}")
    return top


def fetch_one_fulltext(art: dict) -> str:
    """去单条新闻的原网页抓正文。抓到返回正文，任何失败都返回空串（绝不抛错）。"""
    url = art.get("link", "")
    if not url:
        return ""
    try:
        resp = requests.get(
            url,
            timeout=FULLTEXT_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DailyDigestBot/1.0)"},
        )
        resp.raise_for_status()
        # trafilatura 负责从一堆 HTML 里把「文章正文」抠出来，丢掉导航/广告/评论
        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        if not text:
            return ""
        return text.strip()[:FULLTEXT_MAX_CHARS]
    except Exception as e:
        log.warning(f"  抓正文失败 [{art.get('source','')}] {art['title'][:40]}…: {e}")
        return ""


def attach_fulltexts(articles: list[dict]) -> None:
    """并发给每条候选抓正文，写进 art['fulltext']（抓不到就留空，后面回退用摘要）。
    原地修改传入的 articles，不返回新列表。"""
    log.info(f"开始抓 {len(articles)} 条候选的正文全文（并发 {FULLTEXT_WORKERS}）...")
    with ThreadPoolExecutor(max_workers=FULLTEXT_WORKERS) as pool:
        future_map = {pool.submit(fetch_one_fulltext, art): art for art in articles}
        for future in as_completed(future_map):
            art = future_map[future]
            art["fulltext"] = future.result()

    got = sum(1 for a in articles if a.get("fulltext"))
    log.info(f"正文抓取完成：{got}/{len(articles)} 条拿到全文，其余回退用 RSS 摘要")


def summarize_with_deepseek(articles: list[dict]) -> str:
    """把新闻列表发给 DeepSeek，让它用中文总结成每日简报"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("❌ 没有设置 DEEPSEEK_API_KEY，请检查 .env 文件")

    # 构造给 AI 看的新闻列表：优先给抓到的正文全文，没有就退回 RSS 摘要。
    # 同时标注「被几家媒体报道」，让 AI 把热度也纳入精选判断。
    articles_text_parts = []
    for i, art in enumerate(articles, 1):
        parts = [f"{i}. [{art['category']}] {art['title']}"]

        cluster_size = art.get("cluster_size", 1)
        if cluster_size >= 2:
            parts.append(f"   热度: 被 {cluster_size} 家媒体同时报道")

        fulltext = art.get("fulltext", "")
        if fulltext:
            parts.append(f"   正文: {fulltext}")
        elif art["summary"]:
            parts.append(f"   摘要(仅导语，正文未抓到): {art['summary']}")

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
        "【任务】我会给你一批已初筛的候选新闻（约 24 条，已按重要性排过序）。\n"
        "多数条目附带【正文】（从原文抓取的文章本体），少数只有【摘要】。\n"
        "请二次精选，挑出最重要、最有信息量的 15-20 条，编成一份结构化晨报。\n"
        "不重要、过于琐碎或纯软文的，果断舍弃，不要硬凑数量。\n"
        "『被多家媒体同时报道』的条目通常更重要，优先考虑。\n"
        "\n"
        "【怎么用素材——这条最重要】\n"
        "- 有【正文】的：通读后用自己的话写出来龙去脉，提炼正文里的关键事实、数据、人物、因果。这是写出深度的来源，别只看标题。\n"
        "- 只有【摘要】的：据实简写，明确不要脑补正文里没有的细节。\n"
        "- 严禁编造：人名、数字、引语、时间、因果，凡素材里没有的，一律不写。宁可短，不可假。\n"
        "\n"
        "【输出结构】用 Markdown：\n"
        "1. 顶部一段『今日导语』（3-4 句）：概括今天全球最值得关注的主线与基调。\n"
        "2. 分三组（哪组没料就省略该组）：\n"
        "   ## 🌍 国际要闻\n"
        "   ## 💻 科技与 AI\n"
        "   ## 💰 财经市场\n"
        "3. 每条新闻按这个格式写：\n"
        "   **加粗的中文标题**\n"
        "   3-5 句正文：交代背景、关键细节与数据、和它意味着什么（基于【正文】，写出来龙去脉，别只翻译原标题）。\n"
        "   > 【点评】一句你作为主编的判断——影响、看点、或值得警惕之处。\n"
        "4. 结尾写一段『编辑手记 / 今日看点』（3-5 句）：串联今天的脉络，给出前瞻或提醒。\n"
        "\n"
        "【要求】\n"
        "- 全程中文，专有名词首次出现可附英文原名。\n"
        "- 点评要有信息增量和观点，不要写『值得关注』这种空话。\n"
        "- 财经部分尽量点到当日市场情绪/资金面/政策含义。"
    )

    today_str = datetime.now(TZ).strftime("%Y年%m月%d日 %A")
    user_prompt = (
        f"今天是 {today_str}。以下是已初筛的候选新闻（共 {len(articles)} 条），"
        f"请按要求精选并编成今天的深度晨报：\n\n{articles_text}"
    )

    log.info(f"发送 {len(articles)} 条候选到 DeepSeek 进行精选与撰写...")

    # 把「发一次请求」封装成内部函数，外面用重试循环包它。
    # 失败分两类：网络抖动（该重试）vs 请求本身有问题如密钥错误（重试无用，直接抛）。
    def _call_once():
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
                "max_tokens": 6000,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()

    # 「该重试」的瞬时网络错误：连接被掐断、超时、连不上。
    # 注意：raise_for_status() 抛的 HTTPError（如 401/400）不在这里，会直接向上抛——密钥错重试无意义。
    RETRYABLE = (
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    MAX_RETRIES = 3   # 总共最多尝试 3 次（1 次正常 + 2 次重试）

    data = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = _call_once()
            break  # 成功就跳出循环
        except RETRYABLE as e:
            # 最后一次还失败：别再吞了，抛出去让上层知道今天确实没生成成功
            if attempt == MAX_RETRIES:
                log.error(f"DeepSeek 第 {attempt} 次仍失败（{type(e).__name__}: {e}），放弃重试")
                raise
            # 指数退避：第 1 次失败等 2s，第 2 次等 4s，给对端/网络留恢复时间
            wait = 2 ** attempt
            log.warning(
                f"DeepSeek 第 {attempt}/{MAX_RETRIES} 次失败（{type(e).__name__}），"
                f"{wait}s 后重试..."
            )
            time.sleep(wait)

    content = data["choices"][0]["message"]["content"]
    tokens_used = data.get("usage", {}).get("total_tokens", "?")
    log.info(f"DeepSeek 返回 {len(content)} 字，消耗 {tokens_used} tokens")
    return content


def push_to_wechat(content: str, sendkeys: list[str]):
    """通过 Server酱 把内容推送到微信（支持多个 SendKey，每人一个）"""
    if not sendkeys:
        raise RuntimeError("❌ 没有设置 SERVERCHAN_SENDKEY，请检查 .env 文件")

    today_str = datetime.now(TZ).strftime("%m/%d")

    failed = []
    for i, key in enumerate(sendkeys):
        key = key.strip()
        if not key:
            continue
        label = f"收件人{i+1}" if len(sendkeys) > 1 else "微信"
        log.info(f"正在推送到 {label} (Server酱)...")
        try:
            resp = requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={
                    "title": f"📰 每日全球要闻 — {today_str}",
                    "desp": content,
                },
                timeout=30,
            )
            result = resp.json()
            if result.get("code") == 0:
                log.info(f"✅ {label} 推送成功！")
            else:
                log.error(f"❌ {label} 推送失败: {result}")
                failed.append(f"{label}: {result}")
        except Exception as e:
            log.error(f"❌ {label} 推送异常: {e}")
            failed.append(f"{label}: {e}")

    if failed:
        log.warning(f"部分推送失败 ({len(failed)}/{len(sendkeys)}): {'; '.join(failed)}")
    else:
        log.info(f"🎉 全部推送成功（共 {len(sendkeys)} 人）")


# ═══════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════

def main():
    # 让控制台输出统一走 UTF-8，遇到无法显示的字符（如 emoji）就替换而非崩溃。
    # 主要是兼容 Windows 默认的 GBK 终端；Linux/GitHub Actions 本就是 UTF-8，无副作用。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # 某些环境下流不支持 reconfigure，忽略即可

    # 加载上次推送记录，避免早晚报重复
    sent_links = load_sent_links()

    log.info("=" * 50)
    log.info("📡 开始抓取 RSS 新闻...")
    articles = fetch_all_feeds(skip_links=sent_links)
    log.info(f"共抓到 {len(articles)} 条有效新闻（已跳过 {len(sent_links)} 条上次已推送）")

    if not articles:
        log.error("没有抓到任何新闻，退出")
        return

    log.info("📰 抓取候选新闻的正文全文...")
    attach_fulltexts(articles)

    log.info("🤖 调用 DeepSeek 生成中文简报...")
    summary = summarize_with_deepseek(articles)

    # 支持多人推送：用逗号分隔多个 SendKey，每人一个
    sendkeys = [k.strip() for k in SERVERCHAN_SENDKEY.split(",") if k.strip()]
    log.info(f"📲 推送到微信（共 {len(sendkeys)} 人）...")
    push_to_wechat(summary, sendkeys)

    # 保存本次候选链接，下次跑时跳过，避免早晚报重复
    candidate_links = [a["link"] for a in articles if a.get("link")]
    save_sent_links(candidate_links)

    log.info("=" * 50)
    log.info("🎉 全部完成！")

    # 打印摘要到日志（方便在 GitHub Actions 里查看）
    print("\n" + "=" * 50)
    print(summary)
    print("=" * 50)


if __name__ == "__main__":
    main()
