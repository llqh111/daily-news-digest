"""P2-D 证据优先的内容质量增强 - 质量观测与校验。"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import (
    EVIDENCE_VALIDATION_ENABLED,
    EVIDENCE_VALIDATION_MODE,
    UNSUPPORTED_NUMBER_THRESHOLD,
)
from .factcheck import extract_numerical_claims

log = logging.getLogger(__name__)

_ARTICLE_ID_COMMENT_RE = re.compile(r"^[ \t]*<!--\s*article_id:[^>]+-->\s*\r?\n?", re.MULTILINE)
_MAIN_ITEM_END_RE = re.compile(r"(?m)^##\s")


def parse_main_items(markdown_content: str) -> list[dict]:
    """
    从主新闻 Markdown 中提取每条新闻的 article_id 和正文内容。
    返回列表，每个元素形如 {"article_id": "...", "content": "..."}
    """
    items = []
    # 假设新闻条目是以标题行开头的段落，标题行中可能包含注释
    # 例如：
    # <!-- article_id:a1_xxx -->
    # **🔥 标题**
    # 正文内容...

    # 我们按 <!-- article_id: 拆分
    parts = markdown_content.split("<!-- article_id:")
    for part in parts[1:]:
        # 提取 ID
        idx = part.find("-->")
        if idx == -1:
            continue
        article_id = part[:idx].strip()

        # 提取接下来的文本（直到下一个条目或者结束）
        # 这里用一种简单方式：直接拿这部分的全部文字去测，因为已经是分离的块了。
        content = part[idx+3:].strip()
        # split ????? article_id ???????????????????
        # ??????????????????????????
        end_match = _MAIN_ITEM_END_RE.search(content)
        if end_match:
            content = content[:end_match.start()].rstrip()


        items.append({
            "article_id": article_id,
            "content": content
        })

    return items



def strip_internal_article_ids(markdown_content: str) -> str:
    """????????? article_id ????????????????"""
    return _ARTICLE_ID_COMMENT_RE.sub("", markdown_content)


def extract_and_normalize_numbers(text: str) -> list[float]:
    """
    从文本提取数字并进行规范化，转成浮点数，方便进行比较。
    依赖 factcheck.extract_numerical_claims
    """
    claims = extract_numerical_claims(text)
    nums = []

    for c in claims:
        if c["type"] in ("数量", "金额", "百分比"):
            val_str = c["claim"]
            # 提取数字部分
            m = re.search(r"(\d+(?:\.\d+)?)", val_str.replace(",", ""))
            if m:
                try:
                    num_val = float(m.group(1))

                    # 处理量级
                    val_lower = val_str.lower()
                    if "billion" in val_lower or "十亿" in val_lower:
                        num_val *= 1e9
                    elif "million" in val_lower or "百万" in val_lower:
                        num_val *= 1e6
                    elif "trillion" in val_lower or "万亿" in val_lower:
                        num_val *= 1e12
                    elif "万" in val_lower:
                        num_val *= 1e4
                    elif "亿" in val_lower:
                        num_val *= 1e8

                    nums.append(num_val)
                except ValueError:
                    pass
    return nums


def validate_main_digest_evidence(markdown_content: str, evidence_cards: list[dict]) -> dict:
    """
    校验成稿中的事实与证据卡片的一致性。
    生成质量报告。
    """
    if not EVIDENCE_VALIDATION_ENABLED:
        return {}

    items = parse_main_items(markdown_content)
    card_map = {c["article_id"]: c for c in evidence_cards}

    report_items = []
    total_unsupported = 0

    for item in items:
        aid = item["article_id"]
        content = item["content"]

        # 提取成稿数字
        draft_nums = extract_and_normalize_numbers(content)

        # 找对应的 card
        card = card_map.get(aid)
        if not card:
            report_items.append({
                "article_id": aid,
                "has_unsupported_numbers": False,
                "unsupported_list": [],
                "error": "evidence_card_missing"
            })
            continue

        # 提取卡片数字
        evidence_num_texts = card.get("numbers", [])
        evidence_nums = []
        for text in evidence_num_texts:
            evidence_nums.extend(extract_and_normalize_numbers(text))

        # 比较
        unsupported = []
        for dnum in draft_nums:
            # 判断 dnum 是否在 evidence_nums 中（允许 1% 误差）
            found = False
            for enum in evidence_nums:
                if enum == 0 and dnum == 0:
                    found = True
                    break
                elif enum != 0 and abs(dnum - enum) / abs(enum) < 0.01:
                    found = True
                    break
            if not found:
                unsupported.append(dnum)

        if len(unsupported) > UNSUPPORTED_NUMBER_THRESHOLD:
            total_unsupported += 1
            if EVIDENCE_VALIDATION_MODE == "enforce":
                log.warning(f"ENFORCE 阻断: 条目 {aid} 发现无证据支撑的数字: {unsupported}")

        report_items.append({
            "article_id": aid,
            "has_unsupported_numbers": len(unsupported) > 0,
            "unsupported_list": unsupported
        })

    return {
        "total_items": len(items),
        "items_with_unsupported_numbers": total_unsupported,
        "unsupported_ratio": total_unsupported / len(items) if items else 0.0,
        "items": report_items
    }


def summarize_quality_window(reports: list[dict]) -> dict:
    """统计一定时间窗口内的质量报告。"""
    total = 0
    unsupported = 0

    for r in reports:
        total += r.get("total_items", 0)
        unsupported += r.get("items_with_unsupported_numbers", 0)

    return {
        "window_reports": len(reports),
        "total_items": total,
        "total_unsupported": unsupported,
        "overall_unsupported_ratio": unsupported / total if total else 0.0
    }
