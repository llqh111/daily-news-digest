"""triage.py — DeepSeek R1 决策环节：精选 + 排序候选新闻。

从粗筛候选（~25 条）中让 R1 精选并打分，返回排好序的子集（≤ FINAL_PICK 条）。
输入：只用轻量字段（title/summary/source/category/cluster_size），不带正文——正文在下一步才抓。
任何 AI/解析失败 → 回退到粗筛 score 排序，绝不中断主流水线。
"""

from __future__ import annotations

import json
import logging
import re

from .ai import _call_deepseek_once
from .config import FINAL_PICK, TRIAGE_MODEL

log = logging.getLogger(__name__)


def _articles_to_triage_text(articles: list[dict]) -> str:
    """把候选列表转成轻量文本供 R1 决策（不含正文，节省 token）。"""
    parts = []
    for i, art in enumerate(articles, 1):
        lines = [f"{i}. [{art.get('category', '?')}] {art.get('title', '')}"]
        cluster_size = art.get("cluster_size", 1)
        if cluster_size >= 2:
            lines.append(f"   热度：被 {cluster_size} 家媒体同时报道")
        summary = art.get("summary", "")
        if summary:
            lines.append(f"   摘要：{summary[:200]}")
        lines.append(f"   来源：{art.get('source', '')}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def triage_with_deepseek(articles: list[dict]) -> list[dict]:
    """R1 决策：从粗筛候选里精选+排序，返回选中子集。

    成功时：写回 ai_score/ai_reason，过滤 keep=false，按 ai_score 降序，截断到 FINAL_PICK。
    失败时：回退『按粗筛 score 降序取前 FINAL_PICK』，绝不中断流水线。
    """
    if not articles:
        return []

    def _fallback(reason: str) -> list[dict]:
        log.warning(f"⚠️ triage 回退粗筛排序（原因：{reason}）")
        return sorted(articles, key=lambda a: a.get("score", 0), reverse=True)[:FINAL_PICK]

    articles_text = _articles_to_triage_text(articles)
    n = len(articles)

    system_prompt = (
        "你是一位资深中文新闻主编，读者画像：硬核 PC 游戏/MOD 玩家、正在学 AI 编程、身处中国。\n"
        "任务：从以下候选新闻中精选最有价值的条目并打分。\n"
        "【输出要求】只输出纯 JSON 数组，不输出任何其他文字、markdown、代码块或解释。\n"
        "格式：[{\"id\":1,\"keep\":true,\"score\":8,\"reason\":\"≤20字理由\"}, ...]\n"
        "score 范围 1-10（10=必读），reason 不超过 20 个汉字。"
    )
    user_prompt = (
        f"以下是 {n} 条候选新闻，请逐条给出 keep/score/reason 决策：\n\n{articles_text}"
    )

    try:
        data = _call_deepseek_once(system_prompt, user_prompt,
                                   max_tokens=4000, model=TRIAGE_MODEL)
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        return _fallback(f"LLM 调用失败：{e}")

    # ── JSON 解析：用正则抠出第一个 [ … 最后一个 ] 之间的内容 ──
    try:
        m = re.search(r"\[[\s\S]*\]", content)
        if not m:
            return _fallback("LLM 输出中找不到 JSON 数组")
        decisions: list[dict] = json.loads(m.group(0))
    except Exception as e:
        return _fallback(f"JSON 解析失败：{e}")

    # ── 建 id→article 映射，把决策写回 article dict ──
    id_map = {i: art for i, art in enumerate(articles, 1)}
    kept = []
    for d in decisions:
        try:
            art_id = int(d.get("id", 0))
            if art_id not in id_map:
                continue
            if not d.get("keep", False):
                continue
            art = id_map[art_id]
            art["ai_score"] = float(d.get("score", 5))
            art["ai_reason"] = str(d.get("reason", ""))
            kept.append(art)
        except Exception:
            continue

    if not kept:
        return _fallback("LLM 决策后无保留条目")

    # 按 ai_score 降序，截断到 FINAL_PICK
    kept.sort(key=lambda a: a.get("ai_score", 0), reverse=True)
    result = kept[:FINAL_PICK]
    log.info(f"triage 完成：{n} 条 → 精选 {len(result)} 条（模型：{TRIAGE_MODEL}）")
    return result
