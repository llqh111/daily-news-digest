"""自评重写环：DeepSeek 给自己的成稿打分，低于阈值重写一次。

为什么纯 DeepSeek：本项目只有 DeepSeek API，而「让模型当裁判审自己」
（LLM-as-judge）恰好是它擅长、又不需要任何外部依赖的事。

降级哲学（与 triage/scout 一致）：
  · 裁判调用/解析失败 → 视为通过（overall=10），用原稿。
  · 重写调用失败 → 用原稿。
  · 重写后做「来源标记数不降 + 长度不暴跌」硬校验——破格式就弃用重写。
质量增强绝不阻断推送：宁可不改，不可改坏。
"""

from __future__ import annotations

import json
import logging
import re

from .ai import _call_deepseek_once
from .config import CRITIQUE_MODEL, REWRITE_THRESHOLD, SELF_CRITIQUE_ENABLED
from .factcheck import sanity_check_output

log = logging.getLogger(__name__)

_SOURCE_RE = re.compile(r"📰\s*来源")
# 重写稿长度不得低于原稿的此比例，否则视为塌缩、弃用
_REWRITE_MIN_LEN_RATIO = 0.7


def _source_count(text: str) -> int:
    """数「📰 来源」标记条数——重写前后比对，防丢链接。"""
    return len(_SOURCE_RE.findall(text))


def evaluate_digest(summary: str, call=None) -> dict:
    """让 DeepSeek 当裁判给成稿打分。返回 {"overall": float, "issues": [str, ...]}。

    任何调用/解析失败 → 返回 {"overall": 10.0, "issues": []}（视为通过）。
    call 可注入（测试用），默认走真实 _call_deepseek_once。
    """
    call = call or _call_deepseek_once

    system = (
        "你是一位严格的中文新闻主编质检员，审阅一份已成稿的深度晨报。\n"
        "按以下维度找问题（不是夸奖，是挑刺）：\n"
        "1. 信息密度：有没有空话/正确的废话，是否落到具体事实、数据、因果；\n"
        "2. 三段式：【核心事实】【深层逻辑】【后市/影响】是否齐全、各司其职；\n"
        "3. 本土化与个人雷达：对中国/读者的影响是否具体（非泛泛「利好行业」）；\n"
        "4. 重复：是否有两条在讲同一件事；\n"
        "5. 来源：每条是否带「📰 来源」。\n"
        "\n"
        "【输出要求】只输出纯 JSON，不要任何其他文字或代码块：\n"
        '{"overall": 7, "issues": ["问题一", "问题二"]}\n'
        "overall 为 1-10 整体质量分（10=极佳）；issues 列出最该改的问题，最多 5 条，无问题给空数组。"
    )
    user = f"以下是待审阅的晨报全文：\n\n{summary}"

    try:
        data = call(system, user, model=CRITIQUE_MODEL)
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning(f"自评裁判调用失败，视为通过：{type(e).__name__}: {e}")
        return {"overall": 10.0, "issues": []}

    content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
    m = re.search(r"\{[\s\S]*\}", content)
    if m:
        try:
            obj = json.loads(m.group(0))
            return {
                "overall": float(obj.get("overall", 10)),
                "issues": [str(x) for x in obj.get("issues", []) if x],
            }
        except Exception:
            pass
    log.warning(f"自评裁判输出无法解析为 JSON，视为通过。原始输出前 200 字：{content[:200]!r}")
    return {"overall": 10.0, "issues": []}


def revise_digest(summary: str, issues: list[str], call=None) -> str:
    """带着问题清单把成稿重写一遍。返回重写后的全文。

    强约束写进 prompt：保留每条「📰 来源」行原样、不准引入素材外的新事实。
    """
    call = call or _call_deepseek_once
    issues_text = "\n".join(f"- {x}" for x in issues) or "- （未列出具体问题，整体提升信息密度与观点）"

    system = (
        "你是资深中文新闻主编，正在按质检意见修订一份已成稿的晨报。\n"
        "【铁律】\n"
        "· 逐条保留每则新闻末尾的「> 📰 来源：…」行，一字不改，绝不删除或合并；\n"
        "· 保持三大板块标题（## 🌍 国际要闻 / ## 💻 科技与 AI / ## 💰 财经市场）与条目数量不变；\n"
        "· 不准引入原稿/素材里没有的数字、人名、引语——修订是打磨表达与观点，不是再创作；\n"
        "· 只改进点评文字，让其更扎实、更有信息增量、更落到本土化与读者影响。\n"
        "直接输出修订后的完整晨报全文，不要任何额外说明。"
    )
    user = f"【质检意见，请逐条改进】\n{issues_text}\n\n【原稿全文】\n{summary}"

    data = call(system, user, model=CRITIQUE_MODEL, max_tokens=8000)
    return data["choices"][0]["message"]["content"]


def refine_digest(summary: str, *, enabled: bool | None = None,
                  evaluate=None, revise=None) -> str:
    """自评重写主流程：评分 → 低分则重写一次 → 保全校验 → 采用或回退。

    enabled / evaluate / revise 可注入（测试用），默认读 config + 真实实现。
    """
    enabled = SELF_CRITIQUE_ENABLED if enabled is None else enabled
    if not enabled or not summary:
        return summary

    evaluate = evaluate or evaluate_digest
    revise = revise or revise_digest

    try:
        report = evaluate(summary)
    except Exception as e:
        log.warning(f"自评评分异常，跳过重写：{type(e).__name__}: {e}")
        return summary

    overall = report.get("overall", 10)
    log.info(f"📝 自评得分：{overall}（阈值 {REWRITE_THRESHOLD}）")
    if overall >= REWRITE_THRESHOLD:
        return summary

    log.info(f"得分低于阈值，触发重写。问题：{report.get('issues', [])}")
    try:
        rewritten = revise(summary, report.get("issues", []))
    except Exception as e:
        log.warning(f"重写调用失败，回退原稿：{type(e).__name__}: {e}")
        return summary

    # ── 保全校验：破格式就弃用重写 ──
    if _source_count(rewritten) < _source_count(summary):
        log.warning("重写后「📰 来源」标记减少，疑似丢链接 → 回退原稿")
        return summary
    if len(rewritten) < len(summary) * _REWRITE_MIN_LEN_RATIO:
        log.warning("重写后长度暴跌（<70%）→ 回退原稿")
        return summary

    for w in sanity_check_output(rewritten):
        log.warning(f"重写稿 sanity 提示：{w}")
    log.info("✅ 重写稿通过保全校验，采用")
    return rewritten
