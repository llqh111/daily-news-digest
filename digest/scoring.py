"""打分 + 聚类去重 + 分类均衡。

业务逻辑核心：
· score_importance       为单条新闻打"重要性分数"
· extract_proper_nouns   从标题里抽专有名词
· title_keywords         标题分词去虚词
· same_story             两条新闻是否在讲同一件事
· cluster_and_boost      聚类 + 多源印证加分 + 选代表作
· enforce_category_balance  保证每个分类有最低条数
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .config import (
    _HIGH_SIGNAL_RE,
    _MEDIUM_SIGNAL_RE,
    _LOW_VALUE_RE,
    _PERSONAL_RE,
    SOURCE_TRUST,
    CLICKBAIT_PATTERNS,
    TITLE_STOPWORDS,
    MIN_PER_CATEGORY,
)

log = logging.getLogger(__name__)


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

    # ① 关键词信号：命中高/中/负权重词就加减分（整词匹配，避免子串误命中）
    # 语义：每个不同的关键词命中一次只算一次（与旧实现一致），所以用 set 去重。
    score += 3 * len(set(_HIGH_SIGNAL_RE.findall(text)))
    score += 1 * len(set(_MEDIUM_SIGNAL_RE.findall(text)))
    score -= 2 * len(set(_LOW_VALUE_RE.findall(text)))
    score += 4 * len(set(_PERSONAL_RE.findall(text)))

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

        # 记录各家标题 (cluster_titles)
        rep["cluster_titles"] = [f"{m['source']}: {m['title']}" for m in members if m != rep][:4]

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
