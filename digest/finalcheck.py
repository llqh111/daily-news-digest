"""成稿终检：成稿内近重复终检（#2）+ 信息密度地板（#4-A）。

两个特性都遵循设计文档的「降级铁律」：任何失败 / embed 不可用 →
原样返回输入，绝不抛异常、绝不阻断主流水线。语义比对靠本地嵌入
(digest.embedding.embed_titles)，返回的是已 L2 归一化的纯 Python list，
故余弦相似度 = 点积（sum(a*b)），全程不依赖 numpy。
"""

from __future__ import annotations

import logging
import re

from .config import (
    DENSITY_FLOOR_ENABLED,
    DENSITY_MIN_CHARS,
    DENSITY_MIN_FACTS,
    FINAL_DEDUP_THRESHOLD,
    _HARD_NUMBER_RE,
)
from .embedding import embed_titles
from .scoring import extract_proper_nouns

log = logging.getLogger(__name__)

# 渲染后标题行：**<emoji 前缀><中文标题>**。emoji 可叠加且夹杂空格，
# 例如 **🔥⭐⭐🎯 标题** 或 **⭐⭐⭐ 🎯 标题**。
_TITLE_EMOJI = "🔥⭐🎯📈"
_TITLE_LINE_RE = re.compile(rf"^\*\*\s*[{_TITLE_EMOJI}][{_TITLE_EMOJI}\s]*(.+?)\*\*\s*$")
# 来源行：成稿里恒为 "> 📰 来源：…"；放宽到任何含 "📰 来源" 的行以防格式微调。
_SOURCE_LINE_RE = re.compile(r"📰\s*来源")


def _cosine(v1: list[float], v2: list[float]) -> float:
    """两个已 L2 归一化向量的余弦相似度 = 点积。纯 Python，无 numpy。"""
    return sum(a * b for a, b in zip(v1, v2))


def _max_sim_against(cand_vec: list[float], base_vecs: list[list[float]]) -> float:
    """候选向量对一组基准向量的最大余弦相似度。"""
    return max(_cosine(cand_vec, b) for b in base_vecs)


def dedup_secondary(
    news_titles: list[str], gaps: list[dict], bio: dict | None
) -> tuple[list[dict], dict | None]:
    """以主新闻标题为基准向量，剔除 scout(gaps)/bio 中与之近重复的条目。

    命中即丢弃次要板块那一条（主新闻永远保留）。embed 不可用 / 无候选 /
    任何异常 → 原样返回 (gaps, bio)，绝不抛异常、绝不阻断主流水线。
    """
    try:
        if not news_titles:
            return gaps, bio

        base = embed_titles(news_titles)
        if base is None:
            return gaps, bio

        cand_titles = [g["title"] for g in gaps] + ([bio["title"]] if bio else [])
        if not cand_titles:
            return gaps, bio

        cand = embed_titles(cand_titles)
        if cand is None:
            return gaps, bio

        kept_gaps: list[dict] = []
        idx = 0
        for g in gaps:
            sim = _max_sim_against(cand[idx], base)
            if sim < FINAL_DEDUP_THRESHOLD:
                kept_gaps.append(g)
            else:
                log.info(f"终检剔除撞题 scout 条目：{g['title']}")
            idx += 1

        kept_bio = bio
        if bio is not None:
            sim = _max_sim_against(cand[idx], base)
            if sim >= FINAL_DEDUP_THRESHOLD:
                log.info(f"终检剔除撞题 bio 条目：{bio['title']}")
                kept_bio = None

        return kept_gaps, kept_bio
    except Exception as e:  # noqa: BLE001 — 降级铁律：任何失败都退回原输入
        log.warning(f"终检去重失败，跳过（原样返回）：{type(e).__name__}: {e}")
        return gaps, bio


def split_items(summary: str) -> list[dict]:
    """把渲染后的 markdown 成稿切成单条目。

    每条以加粗标题行（**<emoji>标题**）开头，正文若干段，到来源行
    （含 "📰 来源" 的行）结束。返回 [{title, body, source_line}]。
    解析失败 / 无任何标题 → 返回 []（调用方据此跳过）。
    """
    try:
        lines = summary.splitlines()
        items: list[dict] = []
        i = 0
        n = len(lines)
        while i < n:
            m = _TITLE_LINE_RE.match(lines[i].strip())
            if not m:
                i += 1
                continue

            title = m.group(1).strip()
            body_lines: list[str] = []
            source_line = ""
            j = i + 1
            while j < n:
                stripped = lines[j].strip()
                # 撞到下一条标题 → 当前条目正文到此为止（无来源行）。
                if _TITLE_LINE_RE.match(stripped):
                    break
                if _SOURCE_LINE_RE.search(stripped):
                    source_line = stripped
                    j += 1
                    break
                body_lines.append(lines[j])
                j += 1

            body = "\n".join(body_lines).strip()
            items.append({"title": title, "body": body, "source_line": source_line})
            i = j

        return items
    except Exception as e:  # noqa: BLE001 — 解析失败一律降级为空
        log.warning(f"成稿切条失败，跳过：{type(e).__name__}: {e}")
        return []


def density_floor(items: list[dict]) -> list[str]:
    """返回信息密度未达地板的条目标题清单（观察用，调用方只记日志）。

    事实点 = 硬数字命中数 + 专有名词数。正文太短或事实点不足即踩线。
    """
    if not DENSITY_FLOOR_ENABLED:
        return []

    bad: list[str] = []
    for it in items:
        body = it["body"]
        facts = len(_HARD_NUMBER_RE.findall(body)) + len(extract_proper_nouns(body))
        if len(body) < DENSITY_MIN_CHARS or facts < DENSITY_MIN_FACTS:
            bad.append(it["title"])
    return bad
