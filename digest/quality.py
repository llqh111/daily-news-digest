"""P2-D 证据优先的内容质量增强 - 质量观测与校验。"""

from __future__ import annotations

import logging
import re
from collections import Counter
from .config import (
    EVIDENCE_VALIDATION_ENABLED,
    EVIDENCE_VALIDATION_MODE,
    UNSUPPORTED_NUMBER_THRESHOLD,
)

log = logging.getLogger(__name__)


class DigestLayoutError(ValueError):
    """成稿结构损坏时阻止发送。"""


def validate_digest_layout(markdown_content: str, expected_article_ids: list[str] | None = None) -> None:
    """验证发送顺序和条目完整性；在移除内部 article_id 前运行。"""
    errors = []
    ordered_sections = [
        "⏱️ 1 分钟先读", "📊 今日选稿决策", "🌍 国际要闻", "💻 科技与 AI", "💰 财经市场",
        "💡 信息差侦察", "🧬 生物前沿", "🔥 GitHub 热榜", "📡 信号监测",
        "编辑手记", "🎬 今日自媒体选题",
    ]
    positions = []
    for name in ordered_sections:
        if name == "编辑手记":
            matches = list(re.finditer(r"(?m)^(?:『编辑手记[^\n]*』|##\s+编辑手记[^\n]*)", markdown_content))
        else:
            matches = list(re.finditer(rf"(?m)^##\s+{re.escape(name)}[^\n]*$", markdown_content))
        if len(matches) > 1:
            errors.append(f"重复板块：{name}")
        if not matches and name in ("🌍 国际要闻", "💻 科技与 AI", "💰 财经市场"):
            errors.append(f"缺少板块：{name}")
        if matches:
            positions.append(matches[0].start())
    if positions != sorted(positions):
        errors.append("板块顺序错误")
    if re.search(r"自我审计|\{\{NEWS\}\}", markdown_content):
        errors.append("内部审计或占位符泄漏")
    if len(re.findall(r"(?m)^\s*```", markdown_content)) % 2:
        errors.append("代码围栏未闭合")
    in_code = False
    for line in markdown_content.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        elif in_code and (line.startswith("## ") or "<!-- article_id:" in line):
            errors.append("新闻正文被包进代码块")
            break
    if expected_article_ids is not None:
        items = parse_main_items(markdown_content)
        counts = Counter(item["article_id"] for item in items)
        if counts != Counter(expected_article_ids):
            errors.append("主新闻 article_id 缺失、重复或被替换")
        for item in items:
            content = item["content"]
            if not all(label in content for label in ("【核心事实】", "【深层逻辑】", "【后市/影响】")):
                errors.append(f"新闻段落不完整：{item['article_id']}")
            if not re.search(r"(?m)^>\s*📰\s*来源[^\n]*https?://", content):
                errors.append(f"新闻来源缺失：{item['article_id']}")
    if errors:
        raise DigestLayoutError("；".join(errors))

_ARTICLE_ID_COMMENT_RE = re.compile(r"^[ \t]*<!--\s*article_id:[^>]+-->\s*\r?\n?", re.MULTILINE)
_MAIN_ITEM_END_RE = re.compile(r"(?m)^##\s")
_NUMBER_RE = re.compile(
    r"(?<![\d.])(\$?\d{1,3}(?:,\d{3})*(?:\.\d+)?|\$?\d+(?:\.\d+)?)\s*"
    r"(trillion|billion|million|thousand|亿|万|千|美元|元|人|架|%|percent)",
    re.IGNORECASE,
)


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
    nums = []
    for match in _NUMBER_RE.finditer(text):
        raw_number, unit = match.groups()
        number = float(raw_number.replace(",", "").lstrip("$"))
        unit = (unit or "").lower()
        if unit == "trillion" or unit == "万亿":
            number *= 1e12
        elif unit == "billion" or unit == "亿":
            number *= 1e9 if unit == "billion" else 1e8
        elif unit == "million":
            number *= 1e6
        elif unit == "thousand" or unit == "千":
            number *= 1e3
        elif unit == "万":
            number *= 1e4
        nums.append(number)
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
    missing_evidence = 0
    seen_ids = set()

    for item in items:
        aid = item["article_id"]
        seen_ids.add(aid)
        content = item["content"]

        # 提取成稿数字
        draft_nums = extract_and_normalize_numbers(content)

        # 找对应的 card
        card = card_map.get(aid)
        if not card:
            missing_evidence += 1
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

    unmatched_evidence_ids = sorted(set(card_map) - seen_ids)
    return {
        "total_items": len(items),
        "items_with_unsupported_numbers": total_unsupported,
        "items_with_missing_evidence": missing_evidence,
        "unmatched_evidence_ids": unmatched_evidence_ids,
        "validation_complete": not missing_evidence and not unmatched_evidence_ids,
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
