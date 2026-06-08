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
from datetime import datetime, timezone, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
import trafilatura  # 从新闻原网页里提取正文（去广告/导航，只留文章本体）
from dotenv import load_dotenv

# ── 加载 .env 文件中的密钥 ──────────────────────────
load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
CANDIDATE_POOL = 15
# 分类均衡：每个分类至少保留 N 条，不足就补档该分类的次高分条目
MIN_PER_CATEGORY = {"国际": 6, "科技": 4, "财经": 4}
# 抓正文后，正文超过这个长度的条目额外加分（信息密度高）
FULLTEXT_LENGTH_BONUS = 800  # 字符数阈值
FULLTEXT_BONUS_SCORE = 1.5   # 超过阈值加的分
# 抓正文时的并发数和单条超时（秒）。并发快但别太猛，免得被网站当攻击。
FULLTEXT_WORKERS = 6
FULLTEXT_TIMEOUT = 12
# 喂给 AI 的正文最多保留多少字（控制 token 成本，1000 字够写深度了，想更省把数字改小）
FULLTEXT_MAX_CHARS = 1000

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
    # ── 2026-06-07 新增补充源 ──
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "国际"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "category": "国际"},
    {"name": "SCMP", "url": "https://www.scmp.com/rss/91/feed", "category": "国际"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "category": "科技"},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "category": "科技"},
    {"name": "Bloomberg", "url": "https://news.google.com/rss/search?q=when:1d+site:bloomberg.com&hl=en-US&gl=US&ceid=US:en", "category": "财经", "reference": True},
    {"name": "36kr", "url": "https://36kr.com/feed", "category": "科技"},
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
    "earthquake", "typhoon", "flood", "wildfire", "protest", "crackdown",
    # 货币政策与宏观
    "fed", "rate cut", "rate hike", "inflation", "recession", "tariff",
    "central bank", "gdp", "default", "stimulus", "layoff", "bankruptcy",
    # 头部科技 / AI
    "openai", "nvidia", "anthropic", "deepseek", "gpt", "chip ban",
    "semiconductor", "breakthrough", "claude", "gemini", "llm", "agi",
]
# 中权重（+1）：常规但有价值的商业 / 科技 / 市场新闻
MEDIUM_SIGNAL_KEYWORDS = [
    "ai", "apple", "google", "microsoft", "amazon", "tesla", "meta",
    "earnings", "ipo", "merger", "acquisition", "launch", "stocks",
    "market", "oil", "gold", "bitcoin", "lawsuit", "deal", "ban",
    "startup", "funding", "regulation", "antitrust",
    "ev", "battery", "solar", "fusion", "quantum",
]
# 负权重（-2）：标题命中就降权（多半是软新闻 / 娱乐 / 凑数）
LOW_VALUE_KEYWORDS = [
    "recipe", "celebrity", "gossip", "royal", "horoscope", "fashion",
    "recap", "quiz", "best deals", "how to watch", "trailer",
    "tiktok", "viral video", "top 10", "unboxing", "reacts to",
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
    # ── 2026-06-07 新增源 ──
    "Al Jazeera": 2,
    "The Guardian": 2,
    "SCMP": 1,
    "TechCrunch": 1,
    "Wired": 1,
    "Bloomberg": 2,
    "36kr": 1,
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

# 已推送记录文件（避免早晚报重复 + 跨天去重）
SENT_LOG_FILE = os.path.join(os.path.dirname(__file__), "sent_articles.json")
# 跨天去重保留天数：超过这个天数的旧记录自动清理
SENT_RETENTION_DAYS = 7

# 标题聚类时要忽略的高频虚词（这些词到处都是，不能用来判断「是否同一件事」）
TITLE_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "at", "by",
    "with", "from", "as", "is", "are", "was", "were", "be", "been", "will",
    "says", "after", "over", "amid", "into", "out", "up", "new", "us", "uk",
    "report", "live", "news", "video", "watch", "update", "latest",
}


def load_sent_links() -> set[str]:
    """加载跨天推送过的文章链接（合并最近 N 天所有记录），用来跨天去重。"""
    if not os.path.exists(SENT_LOG_FILE):
        return set()
    try:
        with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # ── 兼容旧格式（单次 links 列表，无 history）──
        if "links" in data and "history" not in data:
            links = set(data.get("links", []))
            ts = data.get("ts", "unknown")
            log.info(f"已加载旧格式推送记录：{len(links)} 条（{ts}），下次将自动迁移为新格式")
            return links

        # ── 新格式：按天分桶，合并所有天 → 一个 flat set ──
        history = data.get("history", {})
        all_links: set[str] = set()
        for day, links in history.items():
            all_links.update(links)
            log.debug(f"  加载 {day}: {len(links)} 条")
        log.info(f"已加载跨天推送记录：{len(all_links)} 条（{len(history)} 天窗口）")
        return all_links
    except Exception as e:
        log.warning(f"加载推送记录失败，将按无历史处理: {e}")
        return set()


def save_sent_links(links: list[str]) -> None:
    """保存本次候选文章链接，按天归档，保留最近 N 天，自动清理旧天。"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    # ── 加载现有数据（兼容旧格式）──
    data: dict = {}
    if os.path.exists(SENT_LOG_FILE):
        try:
            with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    # ── 迁移旧格式 ──
    if "links" in data and "history" not in data:
        old_ts = data.get("ts", "unknown")
        # 尝试从 ts 中提取日期（格式: "2026-06-07 13:46"）
        old_day = old_ts[:10] if len(old_ts) >= 10 else "unknown"
        data = {"history": {old_day: data["links"]}}
        log.info(f"已从旧格式迁移：{len(data['history'][old_day])} 条 → {old_day}")

    history = data.get("history", {})

    # ── 写入今天的链接（去重后存）──
    existing = set(history.get(today, []))
    existing.update(links)
    history[today] = list(existing)

    # ── 清理超过保留天数的旧记录 ──
    cutoff = date.today() - timedelta(days=SENT_RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed_days = []
    for day in list(history.keys()):
        if day < cutoff_str:
            removed_days.append(day)
            del history[day]
    if removed_days:
        log.info(f"清理过期记录：{', '.join(sorted(removed_days))}（保留 {SENT_RETENTION_DAYS} 天窗口）")

    # ── 写回 ──
    now = datetime.now(TZ)
    current_session = "AM" if now.hour < 12 else "PM"
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "retention_days": SENT_RETENTION_DAYS,
        "last_session": current_session,
        "history": history,
    }
    try:
        with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        total = sum(len(v) for v in history.values())
        log.info(f"已保存跨天推送记录：{len(links)} 条（今日），总计 {total} 条 / {len(history)} 天")
    except Exception as e:
        log.warning(f"保存推送记录失败（不影响推送）: {e}")


def should_skip_session() -> bool:
    """检查今天同一时段是否已经推送过。防止多个 cron 触发导致重复推送。"""
    now = datetime.now(TZ)
    current_session = "AM" if now.hour < 12 else "PM"
    today_str = now.strftime("%Y-%m-%d")

    if not os.path.exists(SENT_LOG_FILE):
        return False

    try:
        with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 今天还没有推送记录 → 不跳过
        history = data.get("history", {})
        if today_str not in history:
            return False

        # 检查上次推送的时段
        last_session = data.get("last_session", "")
        if last_session == current_session:
            log.info(
                f"⏭️ 今天 {current_session} 时段已推送过"
                f"（{data.get('updated', '?')}），跳过"
            )
            return True

        return False
    except Exception:
        return False


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


def extract_proper_nouns(title: str) -> set[str]:
    """从标题中提取专有名词（首字母大写的词、全大写缩写、中文）。
    用于聚类判断——两个标题共享专有名词，比共享普通词更可能是同一事件。"""
    # 英文专有名词：连续大写字母开头、非句首的单词
    words = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", title)
    # 全大写缩写（2-6 个字母，如 AI, NASA, WTO）
    acronyms = re.findall(r"\b[A-Z]{2,6}\b", title)
    # 中文词（2-6 个汉字连续）
    chinese = re.findall(r"[一-鿿]{2,6}", title)
    result = {w.lower() for w in words if len(w) >= 3}
    result.update(a.lower() for a in acronyms)
    result.update(chinese)
    # 去掉太通用的词
    generic = {"the", "this", "that", "what", "how", "why", "who", "when", "where",
               "new", "world", "news", "report", "says", "will", "can", "may"}
    return {p for p in result if p.lower() not in generic}


def title_keywords(title: str) -> set[str]:
    """把标题拆成「有信息量的关键词集合」，用来判断两条是不是同一件事。
    做法：转小写 → 只留字母数字 → 去掉太短的词和虚词。"""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in TITLE_STOPWORDS}


def same_story(words_a: set[str], words_b: set[str],
               pn_a: set[str] | None = None, pn_b: set[str] | None = None) -> bool:
    """两条标题是否在讲同一件事。

    判断逻辑（按优先级）：
    1. 如果双方都有专有名词且共享 ≥1 个 → 极大概率同事件（如都提到 "Ukraine"）
    2. 如果只有一方有专有名词 → 用共享普通关键词判断（阈值=3）
    3. 都没有专有名词 → 共享关键词 ≥4 才算同事件（更严格，避免误聚类）

    此外，共享数 ≥2 且占了较短标题的一半以上也算——给短标题留空间。"""
    shared = words_a & words_b

    # 专有名词加持
    if pn_a and pn_b:
        # 双方都提取到了专有名词
        shared_pn = pn_a & pn_b
        if shared_pn:
            # 共享专有名词 → 极大加分，降低普通词阈值到 2
            if len(shared) >= 2:
                return True
            # 关键词不够但专有名高度重合也算
            return len(shared_pn) >= 2
        else:
            # 双方都有专有名词但不共享 → 几乎肯定是不同事件
            # 提高阈值到 4，且要求占比较小标题 ≥60%
            if len(shared) >= 4:
                return True
            smaller = min(len(words_a), len(words_b)) or 1
            return len(shared) >= 3 and len(shared) / smaller >= 0.6
    elif not pn_a and not pn_b:
        # 双方都没专有名词 → 严格模式，阈值=4
        if len(shared) >= 4:
            return True
        smaller = min(len(words_a), len(words_b)) or 1
        return len(shared) >= 3 and len(shared) / smaller >= 0.5

    # 回退：一方有专有名词、一方没有（用原始逻辑）
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

    clusters: list[dict] = []   # 每个元素：{"members": [...], "words": 关键词集, "proper_nouns": 专有名词集}
    for art in articles:
        words = title_keywords(art["title"])
        pn = extract_proper_nouns(art["title"])
        placed = False
        for c in clusters:
            if same_story(words, c["words"], pn, c.get("proper_nouns")):
                c["members"].append(art)
                c["words"] |= words
                c["proper_nouns"] |= pn
                placed = True
                break
        if not placed:
            clusters.append({"members": [art], "words": words, "proper_nouns": pn})

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


def enforce_category_balance(articles: list[dict], pool_size: int) -> list[dict]:
    """确保候选池中每个分类都有最低条数。
    不足时，从该分类的次高分条目补档（即使它们排在 pool_size 之后）。
    返回调整后的列表（长度可能超过 pool_size，最多超出各分类缺口之和）。"""
    # 先取 top N
    top = articles[:pool_size]
    rest = articles[pool_size:]

    # 统计各分类现有条数
    cat_counts: dict[str, int] = {}
    for art in top:
        cat = art.get("category", "国际")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    # 补档不足的分类
    for cat, minimum in MIN_PER_CATEGORY.items():
        shortfall = minimum - cat_counts.get(cat, 0)
        if shortfall <= 0:
            continue
        # 从未入选的条目中找该分类的次高分
        fillers = [a for a in rest if a.get("category") == cat and a not in top]
        fillers.sort(key=lambda a: a["score"], reverse=True)
        added = fillers[:shortfall]
        if added:
            top.extend(added)
            names = ", ".join(f"{a['source']}:{a['title'][:20]}" for a in added)
            log.info(f"  分类均衡 → {cat} 补 {len(added)} 条: {names}")

    # 重新按分数排序
    top.sort(key=lambda a: a["score"], reverse=True)
    return top


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

    # 取分数最高的一批代表作，然后用分类均衡补档（保证每类至少有最低条数）
    top = enforce_category_balance(reps, CANDIDATE_POOL)
    log.info(f"粗筛后保留 {len(top)} 条候选")
    if top:
        log.info(f"  分数区间：{top[0]['score']:.1f} ~ {top[-1]['score']:.1f}")
        # 打印分类分布
        cats = {}
        for a in top:
            c = a.get("category", "?")
            cats[c] = cats.get(c, 0) + 1
        log.info(f"  分类分布：{cats}")
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
    同时给抓到长正文的条目额外加分（正文越长 ≈ 信息密度越高）。
    原地修改传入的 articles，不返回新列表。"""
    log.info(f"开始抓 {len(articles)} 条候选的正文全文（并发 {FULLTEXT_WORKERS}）...")
    with ThreadPoolExecutor(max_workers=FULLTEXT_WORKERS) as pool:
        future_map = {pool.submit(fetch_one_fulltext, art): art for art in articles}
        for future in as_completed(future_map):
            art = future_map[future]
            art["fulltext"] = future.result()
            # 正文质量加分：长正文代表深度报道
            ft = art.get("fulltext", "")
            if ft and len(ft) >= FULLTEXT_LENGTH_BONUS:
                art["score"] += FULLTEXT_BONUS_SCORE
                art["fulltext_bonus"] = True

    got = sum(1 for a in articles if a.get("fulltext"))
    bonus = sum(1 for a in articles if a.get("fulltext_bonus"))
    log.info(f"正文抓取完成：{got}/{len(articles)} 条拿到全文（{bonus} 条达深度加分线），其余回退用 RSS 摘要")


# ═══════════════════════════════════════════════════
#  事实核查层：提取数字 → 交叉比对 → 注入提示词
# ═══════════════════════════════════════════════════

def extract_numerical_claims(text: str) -> list[dict]:
    """从正文中提取关键数字声明（人数、金额、百分比、日期）。
    返回带上下文的片段列表，供 AI 交叉比对。"""
    if not text:
        return []

    claims: list[dict] = []

    # 单位数字（人数/伤亡/兵力/就业）
    for m in re.finditer(
        r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*'
        r'(million|billion|trillion|thousand|'
        r'people|dead|killed|injured|wounded|casualties|victims|'
        r'troops|soldiers|jobs|barrels|tons|hectares)',
        text, re.IGNORECASE
    ):
        start = max(0, m.start() - 80)
        ctx = text[start:m.end() + 80].replace('\n', ' ').strip()
        claims.append({"claim": m.group(0), "type": "数量", "context": ctx})

    # 金额（$ 开头）
    for m in re.finditer(
        r'\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion|trillion)?',
        text, re.IGNORECASE
    ):
        ctx = text[max(0, m.start() - 80):m.end() + 80].replace('\n', ' ').strip()
        claims.append({"claim": m.group(0), "type": "金额", "context": ctx})

    # 百分比
    for m in re.finditer(r'(\d{1,3}(?:\.\d+)?)\s*%', text):
        ctx = text[max(0, m.start() - 80):m.end() + 80].replace('\n', ' ').strip()
        claims.append({"claim": m.group(0), "type": "百分比", "context": ctx})

    # 日期（用于检查 AI 不会编造未来日期）
    for m in re.finditer(
        r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
        r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}',
        text
    ):
        ctx = text[max(0, m.start() - 40):m.end() + 40].replace('\n', ' ').strip()
        claims.append({"claim": m.group(0), "type": "日期", "context": ctx})

    # 去重（相同 claim 文本只保留一条）
    seen = set()
    unique: list[dict] = []
    for c in claims:
        if c["claim"].lower() not in seen:
            seen.add(c["claim"].lower())
            unique.append(c)
    return unique[:15]  # 每条最多 15 个声明


def build_factcheck_notes(articles: list[dict]) -> str:
    """生成精简「事实核查备注」追加到 prompt。
    只列出跨文章数字冲突，不列全部声明（控制 prompt 体积）。"""
    all_claims: list[dict] = []

    for art in articles:
        ft = art.get("fulltext", "")
        if not ft:
            continue
        claims = extract_numerical_claims(ft)
        if not claims:
            continue
        for c in claims:
            all_claims.append({"article": art["title"][:60], "claim": c["claim"], "type": c["type"]})

    if len(all_claims) < 2:
        return ""

    # 只做冲突检测，不列全部声明
    conflicts: list[str] = []
    by_type: dict[str, list] = {}
    for c in all_claims:
        by_type.setdefault(c["type"], []).append(c)

    for ctype, items in by_type.items():
        if ctype == "日期" or len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items[i]["article"] == items[j]["article"]:
                    continue
                nums_i = re.findall(r'[\d,]+\.?\d*', items[i]["claim"])
                nums_j = re.findall(r'[\d,]+\.?\d*', items[j]["claim"])
                if nums_i and nums_j:
                    try:
                        vi = float(nums_i[0].replace(',', ''))
                        vj = float(nums_j[0].replace(',', ''))
                        if vi > 0 and vj > 0 and max(vi, vj) / min(vi, vj) > 1.5:
                            conflicts.append(
                                f"  ⚠️ 「{items[i]['article'][:30]}」vs「{items[j]['article'][:30]}」："
                                f"{items[i]['claim']} ↔ {items[j]['claim']}"
                            )
                    except ValueError:
                        pass

    if not conflicts:
        return ""

    return "## ⚠️ 数字冲突提醒（以下来源之间关键数字差异>50%，写作时取多数或最权威来源并注明「数据有出入」）\n" + "\n".join(conflicts[:10])


# ── 分批阈值：每批最多发多少条给 DeepSeek ──
# 太多条 → prompt 大 → 服务端超时掐断。拆成小批每批独立写，最后拼起来。
BATCH_SIZE = 7


def _build_system_prompt(is_batch: bool = False) -> str:
    """构建系统提示词。is_batch=True 时省略「导语」和「编辑手记」——仅写新闻条目。"""
    base = (
        "你是一位资深的中文财经科技新闻主编，每天为高知读者撰写一份深度『晨报』。\n"
        "你的风格像《财新》《FT中文网》：冷静专业，但敢下判断、点出影响与看点，不做无观点的复述。\n"
        "\n"
        "【怎么用素材——这条最重要】\n"
        "- 有【正文】的：通读后用自己的话写出来龙去脉，提炼正文里的关键事实、数据、人物、因果。\n"
        "- 只有【摘要】的：据实简写，明确不要脑补正文里没有的细节。\n"
        "- 严禁编造：人名、数字、引语、时间、因果，凡素材里没有的，一律不写。宁可短，不可假。\n"
        "- 多数来源说法一致的优先采信；若素材间数据冲突，取多数来源说法并注明「多方数据有出入」。\n"
        "- 仅单一来源的独家报道，在点评末尾注明「⚠️ 单一信源」。\n"
        "\n"
        "【每条新闻格式】\n"
        "**🔥/⭐ 中文标题**（🔥=被3+家报道/极高重要性 ⭐⭐⭐=必读 ⭐⭐=值得看 ⭐=速览）\n"
        "3-5 句正文：交代背景、关键细节与数据、和它意味着什么。\n"
        "信息密度标签：📖 深度（有完整正文）或 📡 快讯（仅摘要/参考源）\n"
        "> 【点评】一句主编判断——影响、看点、或值得警惕之处。\n"
        "> 📰 来源：媒体名（原文链接）\n"
        "\n"
        "【要求】\n"
        "- 全程中文，专有名词首次出现可附英文原名。\n"
        "- 点评要有信息增量和观点。\n"
        "- 每条新闻末尾「📰 来源」必须写明媒体名和原文链接——不能省略。\n"
    )

    if is_batch:
        return (
            base
            + "\n【任务】从下面这批新闻中挑出最重要、最有信息量的条目，按上述格式写成新闻简报。"
              "不重要或过于琐碎的舍弃。只输出新闻条目，不要写导语和编辑手记。\n"
        )
    else:
        return (
            base
            + "\n【输出结构】用 Markdown：\n"
              "\n"
              "1. 顶部『今日导语』（3-4 句）：概括今天全球最值得关注的主线与基调。\n"
              "   导语末尾加一行「市场情绪」温度计：🟢 风险偏好 / 🟡 谨慎观望 / 🔴 避险为主（三选一）。\n"
              "\n"
              "2. 分三组（哪组没料就省略该组）：\n"
              "   ## 🌍 国际要闻\n"
              "   ## 💻 科技与 AI\n"
              "   ## 💰 财经市场\n"
              "\n"
              "3. 每条新闻按上述格式写。\n"
              "\n"
              "4. 结尾『编辑手记 / 今日看点』（3-5 句）：串联今天的脉络，给出前瞻或提醒。\n"
              "\n"
              "【事实核查与自我审计——写完必须执行】\n"
              "在「编辑手记」之后，以代码块输出内部审计（简短即可）：\n"
              "```\n"
              "审计: 1.数字均来自素材? 2.无捏造引语? 3.因果均有支撑? 4.单一信源已标? 5.数据冲突已注? 6.热点未遗漏?\n"
              "逐项答「通过」或列出问题。若发现问题，修正正文后再输出。\n"
              "```\n"
              "\n"
              "【任务】我会给你一批已初筛的候选新闻（已按重要性排过序），请按要求精选并编成今天的深度晨报。"
              "不重要、过于琐碎或纯软文的，果断舍弃，不要硬凑数量。\n"
        )


def _articles_to_text(articles: list[dict]) -> str:
    """把文章列表转成发给 AI 的文本块。"""
    parts = []
    for i, art in enumerate(articles, 1):
        p = [f"{i}. [{art['category']}] {art['title']}"]

        cluster_size = art.get("cluster_size", 1)
        if cluster_size >= 2:
            p.append(f"   热度: 被 {cluster_size} 家媒体同时报道")

        fulltext = art.get("fulltext", "")
        if fulltext:
            p.append(f"   正文: {fulltext}")
        elif art["summary"]:
            p.append(f"   摘要(仅导语，正文未抓到): {art['summary']}")

        p.append(f"   来源: {art['source']}")
        if art["link"]:
            p.append(f"   原文链接: {art['link']}")
        parts.append("\n".join(p))
    return "\n\n".join(parts)


def _call_deepseek_once(system_prompt: str, user_prompt: str,
                        max_tokens: int = 3500) -> dict:
    """单次调用 DeepSeek（流式 + 自动重试）。
    返回 {"choices": [{"message": {"content": ...}}], ...} 或抛异常。"""
    RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-reasoner",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": max_tokens,
                },
                timeout=(60, 300),
                stream=True,
            )
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")

            # ── 非流式：temperature 等参数可能导致 DeepSeek 忽略 stream:true，
            #     直接返回 application/json。此时用 resp.json() 解析。
            if "application/json" in ct:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {
                    "choices": [{"message": {"content": content}}],
                    "usage": data.get("usage", {"total_tokens": "?"}),
                }

            # ── 流式：手动拼接 SSE 流式响应
            chunks: list[str] = []
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    delta = json.loads(data_str)
                    choice = delta.get("choices", [{}])[0]
                    chunk_text = choice.get("delta", {}).get("content", "")
                    if chunk_text:
                        chunks.append(chunk_text)
                except Exception:
                    continue
            content = "".join(chunks)
            return {
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": "?"},
            }
        except RETRYABLE as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 5 * (2 ** (attempt - 1))
            log.warning(
                f"DeepSeek 第 {attempt}/{MAX_RETRIES} 次失败（{type(e).__name__}），"
                f"{wait}s 后重试..."
            )
            time.sleep(wait)


def summarize_with_deepseek(articles: list[dict]) -> str:
    """把新闻列表发给 DeepSeek，让它用中文总结成每日简报。
    超过 BATCH_SIZE 条时自动拆批，每批独立写，最后拼成完整晨报。"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("❌ 没有设置 DEEPSEEK_API_KEY，请检查 .env 文件")

    n = len(articles)
    today_str = datetime.now(TZ).strftime("%Y年%m月%d日 %A")

    # ── 不分批：直接一次调用 ──
    if n <= BATCH_SIZE:
        log.info(f"发送 {n} 条候选到 DeepSeek（单批）...")
        system_prompt = _build_system_prompt(is_batch=False)
        articles_text = _articles_to_text(articles)
        factcheck_notes = build_factcheck_notes(articles)
        if factcheck_notes:
            articles_text = articles_text + "\n\n---\n\n" + factcheck_notes
        user_prompt = (
            f"今天是 {today_str}。以下是已初筛的候选新闻（共 {n} 条），"
            f"请按要求精选并编成今天的深度晨报。"
            f"每条新闻末尾务必附上「📰 来源：媒体名（原文链接）」。"
            f"\n\n{articles_text}"
        )
        data = _call_deepseek_once(system_prompt, user_prompt)
        content = data["choices"][0]["message"]["content"]
        log.info(f"DeepSeek 返回 {len(content)} 字")
        _log_sanity(content)
        return content

    # ── 分批模式：拆成多批，每批独立写新闻条目，最后拼起来 ──
    batches = [articles[i:i + BATCH_SIZE] for i in range(0, n, BATCH_SIZE)]
    log.info(f"候选 {n} 条 → 分 {len(batches)} 批发送（每批 ≤{BATCH_SIZE} 条）")

    batch_outputs: list[str] = []
    for bi, batch in enumerate(batches, 1):
        log.info(f"  发送第 {bi}/{len(batches)} 批（{len(batch)} 条）...")
        system_prompt = _build_system_prompt(is_batch=True)
        articles_text = _articles_to_text(batch)

        user_prompt = (
            f"今天是 {today_str}。以下是今天新闻的第 {bi} 批（共 {len(batch)} 条），"
            f"请精选最重要的条目，按格式写成新闻简报。只输出新闻条目，不要导语和编辑手记。"
            f"每条末尾附「📰 来源：媒体名（原文链接）」。"
            f"\n\n{articles_text}"
        )

        data = _call_deepseek_once(system_prompt, user_prompt, max_tokens=3000)
        text = data["choices"][0]["message"]["content"]
        log.info(f"  第 {bi} 批返回 {len(text)} 字")
        batch_outputs.append(text)

    # ── 拼合：用一次短请求让 AI 补导语 + 编辑手记 + 审计 ──
    all_news = "\n\n---\n\n".join(batch_outputs)
    log.info(f"各批合计 {sum(len(o) for o in batch_outputs)} 字，准备拼合并补导语...")

    merge_prompt = (
        "你是一位资深中文新闻主编。以下是将今天各批新闻汇总在一起的简报内容。\n"
        "请为它补上：\n"
        "1. 顶部『今日导语』（3-4 句概括今天全球主线，末尾加市场情绪温度计）\n"
        "2. 结尾『编辑手记 / 今日看点』（3-5 句串联脉络+前瞻）\n"
        "3. 自我审计代码块\n"
        "\n"
        "新闻内容：\n\n"
        f"{all_news}\n\n"
        "请按以下结构输出完整晨报（Markdown）：\n"
        "『今日导语』\n"
        "（市场情绪）\n"
        "## 🌍 国际要闻\n"
        "...（保留上面的新闻条目）\n"
        "## 💻 科技与 AI\n"
        "...\n"
        "## 💰 财经市场\n"
        "...\n"
        "『编辑手记 / 今日看点』\n"
        "（审计代码块）\n"
    )

    # 事实核查笔记也在合并阶段注入
    factcheck_notes = build_factcheck_notes(articles)
    if factcheck_notes:
        merge_prompt = merge_prompt + "\n\n---\n\n⚠️ 事实核查提醒：\n" + factcheck_notes

    log.info("  发送合并请求...")
    data = _call_deepseek_once(
        "你是资深新闻主编，负责为已写好的新闻简报补全导语和编辑手记。保持原文不变，只补充缺失部分。",
        merge_prompt,
        max_tokens=4000,
    )
    final = data["choices"][0]["message"]["content"]
    log.info(f"合并后最终输出 {len(final)} 字")
    _log_sanity(final)
    return final


def _log_sanity(content: str) -> None:
    """输出端轻量扫描：检测常见幻觉模式，在日志中提醒。"""
    warnings = sanity_check_output(content)
    if warnings:
        log.warning(f"⚠️ 幻觉风险提示（{len(warnings)} 项）:")
        for w in warnings:
            log.warning(f"  {w}")


def sanity_check_output(content: str) -> list[str]:
    """对 AI 生成的晨报做轻量事后扫描，检测常见幻觉模式。
    返回警告列表（仅记录日志，不自动修改——人工看日志判断）。"""
    warnings: list[str] = []

    # 1. 检测未来日期（大概率是幻觉）
    today = datetime.now(TZ)
    # 匹配 "2026年6月15日" 之类的日期
    future_dates = re.findall(r'(20\d{2})年(\d{1,2})月(\d{1,2})日', content)
    for y, m, d in future_dates:
        try:
            dt = datetime(int(y), int(m), int(d), tzinfo=TZ)
            if dt > today + timedelta(days=1):
                warnings.append(f"未来日期: {y}年{m}月{d}日（可能为幻觉）")
        except ValueError:
            pass

    # 2. 检测过于精确的数字（如 "3,847,291 人"——AI 很少能造出这种精度的真实数据）
    overly_precise = re.findall(r'\b\d{3,}(?:,\d{3}){2,}\b', content)
    if overly_precise:
        warnings.append(f"高精度数字（疑似编造）: {', '.join(overly_precise[:5])}")

    # 3. 检测常见的 AI 幻觉句式
    hallucination_patterns = [
        (r'据[^，。]+透露', '「据XX透露」格式——确认 XX 是否真实信源'),
        (r'专家(?:分析|认为|指出)', '「专家分析」——确认素材中是否真有专家'),
        (r'据悉[^，。]{10,}', '「据悉」长描述——可能自行脑补'),
    ]
    for pat, desc in hallucination_patterns:
        matches = re.findall(pat, content)
        if len(matches) >= 3:
            warnings.append(f"{desc}（出现 {len(matches)} 次）")

    # 4. 检测是否遗漏了自我审计段
    if '自我审计' not in content:
        warnings.append("缺少「自我审计」段——AI 可能跳过了事实核查步骤")

    # 5. 检查是否有来源链接被省略（格式为 📰 来源：）
    source_count = len(re.findall(r'📰\s*来源', content))
    if source_count < 8:
        warnings.append(f"来源标注仅 {source_count} 条（期望 ≥15，AI 可能省略了来源链接）")

    return warnings


def push_to_wechat(content: str, sendkeys: list[str]):
    """通过 Server酱 把内容推送到微信（支持多个 SendKey，每人一个）。

    内置 3 次重试（指数退避），应对 GitHub Actions 海外 runner 连接国内
    Server酱时偶发的 Connection reset / timeout。
    """
    if not sendkeys:
        raise RuntimeError("❌ 没有设置 SERVERCHAN_SENDKEY，请检查 .env 文件")

    RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    MAX_RETRIES = 3

    today_str = datetime.now(TZ).strftime("%m/%d")

    failed = []
    for i, key in enumerate(sendkeys):
        key = key.strip()
        if not key:
            continue
        label = f"收件人{i+1}" if len(sendkeys) > 1 else "微信"

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                log.info(f"正在推送到 {label} (Server酱) ... 第 {attempt}/{MAX_RETRIES} 次")
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
                    success = True
                    break
                else:
                    log.error(f"❌ {label} 推送失败: {result}")
                    failed.append(f"{label}: {result}")
                    break  # 业务错误不重试
            except RETRYABLE as e:
                if attempt == MAX_RETRIES:
                    log.error(f"❌ {label} 推送异常（已重试 {MAX_RETRIES} 次）: {e}")
                    failed.append(f"{label}: {e}")
                else:
                    wait = 5 * (2 ** (attempt - 1))
                    log.warning(
                        f"⚠️ {label} 推送失败（{type(e).__name__}），"
                        f"{wait}s 后重试..."
                    )
                    time.sleep(wait)
            except Exception as e:
                log.error(f"❌ {label} 推送异常（非网络错误，不重试）: {e}")
                failed.append(f"{label}: {e}")
                break

    if failed:
        log.warning(f"部分推送失败 ({len(failed)}/{len(sendkeys)}): {'; '.join(failed)}")
    else:
        log.info(f"🎉 全部推送成功（共 {len(sendkeys)} 人）")


def push_to_telegram(content: str):
    """通过 Telegram Bot 推送简报。

    Telegram 单条消息上限 4096 字符，晨报通常远超此值 → 按段落边界
    自动分段，每段 ≤ 3500 字符（留安全余量）。多段顺序发送，每段
    之间间隔 0.5s 避免被限速。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("📱 Telegram 未配置，跳过")
        return

    MAX_CHUNK = 3500  # 留余量给标题行和分段标记
    TG_RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    TG_MAX_RETRIES = 3

    # 按双换行（段落边界）切分
    paragraphs = content.split("\n\n")
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 2 <= MAX_CHUNK:
            current = (current + "\n\n" + p) if current else p
        else:
            if current:
                chunks.append(current)
            # 单个段落超限则硬切
            if len(p) > MAX_CHUNK:
                for j in range(0, len(p), MAX_CHUNK):
                    chunks.append(p[j:j + MAX_CHUNK])
                current = ""
            else:
                current = p
    if current:
        chunks.append(current)

    total = len(chunks)
    log.info(f"📱 Telegram 推送（共 {total} 段）...")

    for idx, chunk in enumerate(chunks, 1):
        if total > 1:
            header = f"📰 每日全球要闻 ({idx}/{total})\n\n"
        else:
            header = ""
        text = header + chunk

        for attempt in range(1, TG_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": text,
                    },
                    timeout=30,
                )
                result = resp.json()
                if result.get("ok"):
                    log.info(f"✅ Telegram ({idx}/{total}) 推送成功")
                    break
                else:
                    log.error(
                        f"❌ Telegram ({idx}/{total}) 推送失败: "
                        f"{result.get('description', result)}"
                    )
                    if attempt == TG_MAX_RETRIES:
                        raise RuntimeError(
                            f"Telegram API 返回错误: {result.get('description')}"
                        )
            except TG_RETRYABLE as e:
                if attempt == TG_MAX_RETRIES:
                    log.error(f"❌ Telegram ({idx}/{total}) 推送异常: {e}")
                    raise
                wait = 5 * (2 ** (attempt - 1))
                log.warning(
                    f"⚠️ Telegram ({idx}/{total}) 推送失败 "
                    f"（{type(e).__name__}），{wait}s 后重试..."
                )
                time.sleep(wait)

        if idx < total:
            time.sleep(0.5)  # 段间短暂间隔，避免 Telegram 限速

    log.info("🎉 Telegram 全部推送成功")


# ═══════════════════════════════════════════════════
#  失败告警：流水线任何环节崩了，推送简短告警
# ═══════════════════════════════════════════════════

def _send_alert_summary(msgs: list[str]) -> str:
    """汇总多条告警发送结果，用于日志。"""
    if not msgs:
        return "✅ 全部告警通道已发送"
    return "部分告警通道失败: " + "; ".join(msgs)


def send_failure_alert(error_msg: str, stage: str = "未知") -> None:
    """流水线失败时，向所有可用通道推送简短告警。

    不会抛异常——告警本身失败了也不影响主流程日志。
    """
    now_str = datetime.now(TZ).strftime("%m/%d %H:%M")
    title = f"⚠️ 每日要闻推送失败 — {now_str}"
    body = (
        f"## ⚠️ 每日全球要闻 — 推送失败\n\n"
        f"**失败环节**: {stage}\n"
        f"**时间**: {now_str}\n\n"
        f"**错误信息**:\n"
        f"```\n{error_msg[:800]}\n```\n\n"
        f"请检查 GitHub Actions 日志：\n"
        f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions\n\n"
        f"---\n"
        f"📡 由 Daily News Digest 失败告警自动发送"
    )

    failed: list[str] = []

    # ── Server酱 ──
    if SERVERCHAN_SENDKEY:
        sendkeys = [k.strip() for k in SERVERCHAN_SENDKEY.split(",") if k.strip()]
        for key in sendkeys:
            try:
                resp = requests.post(
                    f"https://sctapi.ftqq.com/{key}.send",
                    data={"title": title, "desp": body},
                    timeout=15,
                )
                result = resp.json()
                if result.get("code") == 0:
                    log.info(f"✅ 失败告警已通过 Server酱 发送")
                else:
                    failed.append(f"Server酱: {result}")
            except Exception as e:
                failed.append(f"Server酱: {e}")

    # ── Telegram ──
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_text = (
                f"⚠️ <b>每日要闻推送失败</b>\n\n"
                f"<b>失败环节</b>: {stage}\n"
                f"<b>时间</b>: {now_str}\n\n"
                f"<b>错误</b>:\n<pre>{error_msg[:500]}</pre>\n\n"
                f"<a href=\"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions\">查看 Actions 日志</a>"
            )
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": tg_text,
                    "parse_mode": "HTML",
                },
                timeout=15,
            )
            result = resp.json()
            if result.get("ok"):
                log.info(f"✅ 失败告警已通过 Telegram 发送")
            else:
                failed.append(f"Telegram: {result.get('description', result)}")
        except Exception as e:
            failed.append(f"Telegram: {e}")

    log.info(f"失败告警发送完成: {_send_alert_summary(failed)}")


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

    # 同日同时段去重：防止多个 cron 触发导致重复推送
    if should_skip_session():
        log.info("🎉 本次运行已跳过（同日同时段已完成推送）")
        return

    # 加载跨天推送记录，用于去重
    sent_links = load_sent_links()

    try:
        log.info("=" * 50)
        log.info("📡 开始抓取 RSS 新闻...")
        articles = fetch_all_feeds(skip_links=sent_links)
        log.info(f"共抓到 {len(articles)} 条有效新闻（已跳过 {len(sent_links)} 条历史已推送）")

        if not articles:
            raise RuntimeError("没有抓到任何新闻——所有 RSS 源均无新内容或全部被过滤/去重跳过")

        log.info("📰 抓取候选新闻的正文全文...")
        attach_fulltexts(articles)

        log.info("🤖 调用 DeepSeek 生成中文简报...")
        summary = summarize_with_deepseek(articles)

        # ── 多渠道推送 ──────────────────────────────────
        # Server酱（国内 → 微信）：GitHub Actions 海外 runner 可能被墙
        if SERVERCHAN_SENDKEY:
            sendkeys = [k.strip() for k in SERVERCHAN_SENDKEY.split(",") if k.strip()]
            log.info(f"📲 推送到微信 Server酱（共 {len(sendkeys)} 人）...")
            push_to_wechat(summary, sendkeys)
        else:
            log.info("📲 Server酱 未配置，跳过微信推送")

        # Telegram：海外 runner 直连，不受 GFW 影响
        push_to_telegram(summary)

        # 保存本次候选链接，跨天去重
        candidate_links = [a["link"] for a in articles if a.get("link")]
        save_sent_links(candidate_links)

        log.info("=" * 50)
        log.info("🎉 全部完成！")

        # 打印摘要到日志（方便在 GitHub Actions 里查看）
        print("\n" + "=" * 50)
        print(summary)
        print("=" * 50)

    except Exception as e:
        # ── 失败告警：推送简短告警到所有可用通道 ──
        err_msg = f"{type(e).__name__}: {e}"
        log.error(f"💥 流水线失败: {err_msg}")
        import traceback
        log.error(traceback.format_exc())

        # 推断失败阶段
        stage_map = {
            "fetch_all_feeds": "RSS抓取",
            "attach_fulltexts": "正文抓取",
            "summarize_with_deepseek": "DeepSeek AI总结",
            "push_to_wechat": "微信推送",
            "push_to_telegram": "Telegram推送",
            "save_sent_links": "记录保存",
        }
        stage = "未知环节"
        tb = traceback.format_exc()
        for func, label in stage_map.items():
            if func in tb:
                stage = label
                break

        try:
            send_failure_alert(err_msg, stage)
        except Exception as alert_err:
            log.error(f"发送失败告警本身也失败了: {alert_err}")

        # 重新抛出，让 GitHub Actions 知道这次运行失败了
        raise


if __name__ == "__main__":
    main()
