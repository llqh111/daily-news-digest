"""抓取层：RSS 并发下载 + 正文 trafilatura 提取。

设计要点：
· 下载与解析分离 —— 下载并发、解析串行（feedparser 写全局去重 set 不是线程安全）
· 任何单源失败/超时都返回 None，绝不向上抛 —— 一个挂掉的源不能拖死整个 job
· 正文抓取也用线程池，但 trafilatura 是 CPU 密集（lxml C 扩展），收益边际
· 去重状态（标题/链接 set）是 fetch_all_feeds 函数内局部 —— 同进程多次
  调用互不污染
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import feedparser
import requests
import trafilatura

from .config import (
    RSS_FEEDS,
    MAX_PER_FEED,
    TIME_WINDOW_HOURS,
    CANDIDATE_POOL,
    FULLTEXT_LENGTH_BONUS,
    FULLTEXT_BONUS_SCORE,
    FULLTEXT_WORKERS,
    FULLTEXT_TIMEOUT,
    FULLTEXT_MAX_CHARS,
    FEED_FETCH_TIMEOUT,
    FEED_FETCH_WORKERS,
)
from .scoring import (
    score_importance,
    cluster_and_boost,
    enforce_category_balance,
)

log = logging.getLogger(__name__)


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


def _fetch_feed_content(feed_info: dict) -> bytes | None:
    """下载单个 RSS 源的原始字节。带超时 + UA，任何失败返回 None（绝不抛错）。

    把"下载"和"解析"分离，使下载可以并发、解析保持串行——
    解析阶段会写去重 set（seen_titles/seen_links），串行才线程安全。
    """
    url = feed_info.get("url", "")
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            timeout=FEED_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DailyDigestBot/1.0)"},
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log.warning(f"抓取失败 {feed_info.get('name', '?')}: {e}")
        return None


def fetch_all_feeds(skip_links: set[str] | None = None) -> list[dict]:
    """抓取所有 RSS 源 → 时间窗口过滤 → 打分 → 同题聚类去重 → 取分数最高的一批候选"""
    if skip_links is None:
        skip_links = set()
    now_utc = datetime.now(timezone.utc)
    articles: list[dict] = []

    # 本轮去重状态：标题/链接。改为函数内局部（原来是模块级全局，
    # 同进程多次调用会被上一轮污染）。解析串行，无需线程安全考量。
    seen_titles: set[str] = set()
    seen_links: set[str] = set()

    # ── 并发下载所有源的原始内容（带超时），再串行解析 ──
    # 用源在 RSS_FEEDS 中的下标做 key（name 理论上可能重名，下标绝不重复）。
    contents: dict[int, bytes | None] = {}
    with ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
        future_map = {
            pool.submit(_fetch_feed_content, fi): idx
            for idx, fi in enumerate(RSS_FEEDS)
        }
        for fut in as_completed(future_map):
            contents[future_map[fut]] = fut.result()

    for idx, feed_info in enumerate(RSS_FEEDS):
        raw = contents.get(idx)
        if raw is None:
            continue  # 下载失败/超时，已在 _fetch_feed_content 里记日志
        feed = feedparser.parse(raw)

        limit = feed_info.get("max_items", MAX_PER_FEED)
        count = 0
        for entry in feed.entries:
            if count >= limit:
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
            if title_lower in seen_titles or (link and link in seen_links):
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

            seen_titles.add(title_lower)
            if link:
                seen_links.add(link)

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
        cats: dict[str, int] = {}
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
