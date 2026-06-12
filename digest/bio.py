"""bio.py — 生物前沿单槽挑选器（每期精确 1 条）。

独立板块，与主新闻三大类（国际/科技/财经）完全隔离：
  抓 BIO_FEEDS → 按 BIO_KEYWORDS 打分 → 取最高 1 条 → AI 写中文一句话概括
  → main.py 注入 🧬 板块。

为什么单开模块而非塞进主打分：
  · 「一次一篇」要的是精确 1 条，打分/配额只能保证 ≥1，保证不了 ==1；
  · 生物词若混进通用关键词表，会去抢国际/科技/财经的名额，破坏既有平衡；
  · 与 scout 同构：独立成板块、容错优先、任何异常都不拖垮主推送。

容错：任何异常 → 返回 None，绝不抛给主流水线。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import feedparser

from .ai import _call_deepseek_once
from .config import (
    BIO_ENABLED,
    BIO_FEEDS,
    BIO_MAX_PER_FEED,
    BIO_MODEL,
    BIO_SOURCE_TRUST,
    BIO_TIME_WINDOW_HOURS,
    _BIO_SIGNAL_RE,
    FEED_FETCH_WORKERS,
)
from .fetch import _fetch_feed_content, clean_html, parse_published

log = logging.getLogger(__name__)


def _score_bio(title: str, summary: str, published: datetime | None, source: str) -> float:
    """给一条生物候选打分：关键词命中为主，新鲜度 + 源信任为辅。

    与主流程 score_importance 刻意分开——只认 BIO_KEYWORDS，不掺通用信号词。
    """
    text = f"{title} {summary}".lower()
    score = 0.0

    # ① 生物信号词：整词匹配，每个不同词只算一次（set 去重，防刷分）
    score += 3 * len(set(_BIO_SIGNAL_RE.findall(text)))

    # ② 新鲜度：越新越加分（科研窗口宽，这里给的加分也温和）
    if published is not None:
        hours_old = (datetime.now(timezone.utc) - published).total_seconds() / 3600
        if hours_old <= 24:
            score += 2
        elif hours_old <= 48:
            score += 1

    # ③ 源信任
    score += BIO_SOURCE_TRUST.get(source, 0)
    return score


def _collect_bio_candidates() -> list[dict]:
    """并发抓所有 BIO_FEEDS，时间窗口过滤 + 打分，返回候选列表（未排序）。"""
    now_utc = datetime.now(timezone.utc)
    candidates: list[dict] = []
    seen_titles: set[str] = set()

    # 并发下载（复用主流程的下载函数，自带超时 + 失败返回 None）
    contents: dict[int, bytes | None] = {}
    with ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
        future_map = {
            pool.submit(_fetch_feed_content, fi): idx
            for idx, fi in enumerate(BIO_FEEDS)
        }
        for fut in as_completed(future_map):
            contents[future_map[fut]] = fut.result()

    for idx, feed_info in enumerate(BIO_FEEDS):
        raw = contents.get(idx)
        if raw is None:
            continue
        feed = feedparser.parse(raw)

        count = 0
        for entry in feed.entries:
            if count >= BIO_MAX_PER_FEED:
                break
            title = entry.get("title", "").strip()
            if not title:
                continue
            title_lower = title.lower()
            if title_lower in seen_titles:
                continue

            summary = clean_html(entry.get("summary", entry.get("description", "")))
            published = parse_published(entry)

            # 时间窗口过滤（有发布时间才判断；没时间的保留）
            if published is not None:
                hours_old = (now_utc - published).total_seconds() / 3600
                if hours_old > BIO_TIME_WINDOW_HOURS:
                    continue

            seen_titles.add(title_lower)
            candidates.append({
                "title": title,
                "summary": summary[:500],
                "link": entry.get("link", "").strip(),
                "source": feed_info["name"],
                "published": published,
                "score": _score_bio(title, summary, published, feed_info["name"]),
            })
            count += 1

    return candidates


def _summarize_bio_zh(art: dict) -> str:
    """让 V3 用一句中文概括这条生物突破。失败 → 回退用原摘要截断。"""
    system_prompt = (
        "你是科普编辑。用一句话（40 字以内）中文概括这则生物/医学研究进展："
        "点明『做了什么 + 为何重要』。只输出这一句话，不要任何前缀、标点编号或解释。"
    )
    user_prompt = f"标题：{art.get('title', '')}\n摘要：{art.get('summary', '')[:500]}"
    try:
        data = _call_deepseek_once(system_prompt, user_prompt,
                                   max_tokens=200, model=BIO_MODEL)
        line = data["choices"][0]["message"]["content"].strip()
        # 去掉模型可能多带的引号/换行
        line = re.sub(r"\s+", " ", line).strip(' "“”')
        if line:
            return line
    except Exception as e:
        log.warning(f"生物一句话概括失败（{type(e).__name__}: {e}），回退用原摘要")
    return art.get("summary", "")[:80]


def pick_bio_breakthrough() -> dict | None:
    """生物板块入口：挑 1 条最高分突破，附 AI 中文一句话概括。

    返回 {title, summary_zh, url, source} 或 None（无候选/被关闭/任何异常）。
    """
    if not BIO_ENABLED:
        log.info("ℹ️ 生物板块已关闭（BIO_ENABLED=False），跳过")
        return None

    try:
        candidates = _collect_bio_candidates()
        if not candidates:
            log.info("🧬 生物板块：未抓到候选，跳过")
            return None

        best = max(candidates, key=lambda a: a["score"])
        log.info(f"🧬 生物板块选中：[{best['source']}] {best['title'][:50]}（分 {best['score']:.1f}）")

        return {
            "title":      best["title"],
            "summary_zh": _summarize_bio_zh(best),
            "url":        best.get("link", ""),
            "source":     best["source"],
        }
    except Exception as e:
        log.warning(f"⚠️ 生物板块整体失败（{type(e).__name__}: {e}），已跳过")
        return None
