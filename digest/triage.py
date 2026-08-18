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
from .config import (
    CATEGORY_FLOOR,
    CATEGORY_QUOTA,
    FINAL_PICK,
    MIN_MAIN_NEWS,
    READER_PROFILE,
    REQUIRED_MAIN_CATEGORIES,
    TRIAGE_MODEL,
)

log = logging.getLogger(__name__)


class SelectionCoverageError(RuntimeError):
    """Raised when a digest would be too thin or miss a required news category."""


def _coverage_problems(articles: list[dict]) -> list[str]:
    """Return unmet delivery-contract requirements without changing the selection."""
    counts = {category: 0 for category in REQUIRED_MAIN_CATEGORIES}
    for article in articles:
        category = article.get("category")
        if category in counts:
            counts[category] += 1

    problems = []
    if len(articles) < MIN_MAIN_NEWS:
        problems.append(f"主新闻仅 {len(articles)} 条（最低 {MIN_MAIN_NEWS} 条）")
    missing = [category for category, count in counts.items() if count == 0]
    if missing:
        problems.append(f"缺少板块：{'、'.join(missing)}")
    return problems


def ensure_main_news_coverage(articles: list[dict]) -> None:
    """Refuse delivery when the selected main-news set cannot meet the reader contract."""
    problems = _coverage_problems(articles)
    if problems:
        raise SelectionCoverageError("；".join(problems))


def _backfill_delivery_coverage(selected: list[dict], all_articles: list[dict]) -> list[dict]:
    """Use ranked same-run candidates when a valid LLM response under-selects news.

    This is deliberately narrower than the normal fallback: it only revives candidates
    when the final selection would otherwise fail the pre-delivery quality contract.
    """
    if not _coverage_problems(selected):
        return selected
    # Do not mutate an already-insufficient source pool: the delivery gate should
    # still alert when upstream fetching genuinely cannot satisfy the contract.
    if _coverage_problems(all_articles):
        return selected

    result = list(selected)
    selected_ids = {id(article) for article in result}
    counts = {category: 0 for category in CATEGORY_QUOTA}
    for article in result:
        category = article.get("category")
        if category in counts:
            counts[category] += 1

    candidates = sorted(
        [article for article in all_articles if id(article) not in selected_ids],
        key=lambda article: article.get("score", 0),
        reverse=True,
    )

    def add(article: dict) -> None:
        article["ai_score"] = float(article.get("score", 0))
        article["ai_reason"] = "R1 覆盖补充：避免主新闻未达送达标准"
        article["triage_mode"] = "coverage_backfill"
        result.append(article)
        selected_ids.add(id(article))
        counts[article["category"]] += 1

    # Required categories take precedence over count, while preserving each category cap.
    for category in REQUIRED_MAIN_CATEGORIES:
        if counts.get(category, 0):
            continue
        for article in candidates:
            if article.get("category") == category and counts[category] < CATEGORY_QUOTA[category]:
                add(article)
                break

    # A complete but too-thin selection is filled from the highest-ranked eligible items.
    for article in candidates:
        if len(result) >= MIN_MAIN_NEWS:
            break
        category = article.get("category")
        if (
            id(article) not in selected_ids
            and category in CATEGORY_QUOTA
            and counts[category] < CATEGORY_QUOTA[category]
        ):
            add(article)

    remaining = _coverage_problems(result)
    if remaining:
        log.warning("triage 覆盖补充后仍未达标：%s", "；".join(remaining))
    else:
        log.warning("triage 覆盖补充：%d 条 -> %d 条", len(selected), len(result))

    result.sort(key=lambda article: article.get("ai_score", article.get("score", 0)), reverse=True)
    return result


def _select_by_category_quota(kept: list[dict], all_articles: list[dict]) -> list[dict]:
    """最终选稿按分类「各排各的」：每类只在【本类内】按 ai_score 选满 CATEGORY_QUOTA[cat]（上限封顶），
    跨类零竞争——科技/财经再热也吃不掉国际的名额。国际/财经入选不足 CATEGORY_FLOOR 时，
    从【本类】原始候选按粗筛分补足（地缘/宏观保覆盖；只补本类、不复活被 AI 毙掉的条目）。

    返回整体按 ai_score（无则粗筛 score）降序的结果，长度 ≤ Σ CATEGORY_QUOTA = FINAL_PICK。
    """
    # 本类已入选（AI keep=true）按 ai_score 降序分桶
    by_cat: dict[str, list[dict]] = {}
    for a in kept:
        by_cat.setdefault(a.get("category", ""), []).append(a)
    for bucket in by_cat.values():
        bucket.sort(key=lambda a: a.get("ai_score", 0), reverse=True)

    result: list[dict] = []
    result_ids: set[int] = set()
    for cat, quota in CATEGORY_QUOTA.items():
        chosen = by_cat.get(cat, [])[:quota]          # 上限：本类最多拿 quota 条
        for a in chosen:
            result.append(a)
            result_ids.add(id(a))

        # 下限：仅国际/财经，且只从【本类】原始候选补（按粗筛 score，不跨类）
        floor = CATEGORY_FLOOR.get(cat, 0)
        if len(chosen) < floor:
            backfill = sorted(
                [a for a in all_articles
                 if a.get("category") == cat and id(a) not in result_ids],
                key=lambda a: a.get("score", 0), reverse=True,
            )
            for extra in backfill[: floor - len(chosen)]:
                log.info(f"分类下限补充：{cat} 补入「{extra.get('title', '')[:30]}」")
                result.append(extra)
                result_ids.add(id(extra))

    # 呈现顺序：整体按 ai_score（补档项无 ai_score 则退回粗筛 score）降序
    result.sort(key=lambda a: a.get("ai_score", a.get("score", 0)), reverse=True)
    return _backfill_delivery_coverage(result, all_articles)


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


def _extract_decisions(content: str) -> list[dict]:
    """从 R1 输出里抠出决策列表，对截断/夹带文字尽量鲁棒。

    三级回退：
      ① ```json 代码块里的数组 → 整体 json.loads
      ② 裸数组（首个 [ 到末个 ]）→ 整体 json.loads
      ③ 上面都失败（多半是 max_tokens 截断、数组没闭合的 ]）→
         逐个抠 {…} 决策对象单独解析，截断只丢最后半条，不整盘报废。
    返回决策 dict 列表（可能为空，交由调用方决定是否 fallback）。
    """
    content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
    # ① / ② 先试整体数组
    m = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", content)
    if not m:
        m = re.search(r"\[[\s\S]*\]", content)
    if m:
        try:
            parsed = json.loads(m.group(1) if m.lastindex else m.group(0))
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass  # 落到 ③ 逐对象救活

    # ③ 抗截断：决策对象无嵌套花括号，逐个抠出来单独 json.loads
    salvaged: list[dict] = []
    for obj_str in re.findall(r"\{[^{}]*\}", content):
        try:
            obj = json.loads(obj_str)
        except Exception:
            continue
        if isinstance(obj, dict) and "id" in obj:
            salvaged.append(obj)
    return salvaged


def triage_with_deepseek(articles: list[dict]) -> list[dict]:
    """R1 决策：从粗筛候选里精选+排序，返回选中子集。

    成功时：写回 ai_score/ai_reason，过滤 keep=false，按 ai_score 降序，截断到 FINAL_PICK。
    失败时：回退『按粗筛 score 降序取前 FINAL_PICK』，绝不中断流水线。
    """
    if not articles:
        return []

    def _fallback(reason: str) -> list[dict]:
        log.warning(f"⚠️ triage 回退粗筛排序（原因：{reason}）")
        fallback = sorted(articles, key=lambda a: a.get("score", 0), reverse=True)[:FINAL_PICK]
        for article in fallback:
            # Keep the decision table and its audit trail intact even when R1 is unavailable.
            article["ai_score"] = float(article.get("score", 0))
            article["ai_reason"] = f"R1 决策回退：{reason}"
            article["triage_mode"] = "fallback"
        return fallback

    articles_text = _articles_to_triage_text(articles)
    n = len(articles)

    system_prompt = (
        "你是一位资深中文新闻主编。读者画像：\n"
        f"{READER_PROFILE}\n"
        "\n"
        "任务：从以下候选新闻中精选最有价值的条目并打分。\n"
        "\n"
        "【分类配额（最重要）】最终按分类各排各的选稿，请确保每类都留够候选：\n"
        f"· 国际要闻：尽量保留约 {CATEGORY_QUOTA['国际']} 条（地缘政治/宏观事件必须覆盖，至少 {CATEGORY_FLOOR['国际']} 条）\n"
        f"· 科技与 AI：尽量保留约 {CATEGORY_QUOTA['科技']} 条\n"
        f"· 财经市场：尽量保留约 {CATEGORY_QUOTA['财经']} 条（含币圈/股票/投资/理财，至少 {CATEGORY_FLOOR['财经']} 条）\n"
        "如果某分类候选不足上述数量，则全部保留。\n"
        "\n"
        "【输出要求】只输出纯 JSON 数组，不输出任何其他文字、markdown、代码块或解释。\n"
        "格式：[{\"id\":1,\"keep\":true,\"score\":8,\"reason\":\"≤20字理由\"}, ...]\n"
        "score 范围 1-10（10=必读），reason 不超过 20 个汉字。"
    )
    user_prompt = (
        f"以下是 {n} 条候选新闻，请逐条给出 keep/score/reason 决策：\n\n{articles_text}"
    )

    try:
        # max_tokens 8000：候选变多后 R1 的 JSON 决策更长，4000 易截断成残缺数组 →
        # 解析失败整盘退回粗筛。给足预算优先保住语义精选。
        data = _call_deepseek_once(system_prompt, user_prompt,
                                   max_tokens=8000, model=TRIAGE_MODEL)
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        return _fallback(f"LLM 调用失败：{e}")

    # ── JSON 解析：代码块数组 / 裸数组 / 截断后逐对象救活（见 _extract_decisions）──
    decisions = _extract_decisions(content)
    if not decisions:
        # 记录原始输出，否则无法诊断 R1 到底吐了什么（对齐 scout.py 的可观测性）
        log.warning(f"triage 解析不出决策，R1 原始输出（前 300 字）：{content[:300]!r}")
        return _fallback("LLM 输出中找不到可解析的决策")

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

    # 最终选稿：分类硬分桶「各排各的」（上限封顶 + 国际/财经下限保障），跨类零竞争
    result = _select_by_category_quota(kept, articles)

    log.info(f"triage 完成：{n} 条 → 精选 {len(result)} 条（模型：{TRIAGE_MODEL}）")
    return result
