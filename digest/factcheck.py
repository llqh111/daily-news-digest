"""事实核查层：入参数字提取、跨文章冲突检测、输出端 sanity_check。

三层防幻觉链条中的两层在这里实现：
· 入参层：build_factcheck_notes 把跨文章数字冲突注入 prompt
· 输出层：sanity_check_output 对 AI 生成稿做轻量事后扫描

（生成层的"自我审计"在 ai.py 的 prompt 里强制要求 AI 写。）
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from .config import TZ

log = logging.getLogger(__name__)


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

    # 6. 检查进展比例
    progress = len(re.findall(r'📈', content))
    total_items = source_count or 1
    if progress / total_items > 0.5:
        warnings.append(f"📈进展条目占比 {progress}/{total_items} 过半，疑似误接旧事件")

    return warnings
