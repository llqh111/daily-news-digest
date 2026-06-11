"""topics.py — 每日自媒体选题生成（3 个，基于今日新闻 + 侦察兵发现）。

调 R1 生成 ## 🎬 今日自媒体选题，强调时效性 + 留住观众的钩子 + 质量保障。
失败返回空串，绝不中断主推送。
"""

from __future__ import annotations

import logging
from datetime import datetime

from .ai import _call_deepseek_once
from .config import TRIAGE_MODEL, TZ, TOPIC_PLATFORM

log = logging.getLogger(__name__)


def _build_topics_user_prompt(articles: list[dict], gaps: list[dict]) -> str:
    """构建选题生成的 user prompt。"""
    today_str = datetime.now(TZ).strftime("%Y年%m月%d日")
    lines = [f"今天是 {today_str}。请基于以下今日真实新闻和侦察发现，生成 3 个自媒体选题。\n"]

    if articles:
        lines.append("【今日精选新闻】")
        for i, art in enumerate(articles[:13], 1):
            reason = art.get("ai_reason", "")
            reason_str = f"（{reason}）" if reason else ""
            lines.append(f"{i}. [{art.get('category','')}] {art.get('title','')}{reason_str}")
            if art.get("summary"):
                lines.append(f"   {art['summary'][:100]}")
        lines.append("")

    if gaps:
        lines.append("【侦察兵发现（低曝光高价值内容）】")
        for i, g in enumerate(gaps, 1):
            lines.append(f"{i}. {g.get('title','')}")
            lines.append(f"   为何有价值：{g.get('why_valuable','')}")
            lines.append(f"   链接：{g.get('url','')}")
        lines.append("")

    return "\n".join(lines)


_TOPICS_SYSTEM = """你是一位短视频/公众号内容策划专家。基于今日真实新闻，生成 3 个自媒体选题。

【平台口吻】{platform}

【每个选题必须包含以下格式，不可省略任何字段】：
### 选题 N：[选题标题]
🪝 **钩子/前3秒**：[让人必须继续看的开场，制造悬念/反常识/强利益点]
⏰ **为什么是现在**：[基于今日新闻，点明时效性]
🎯 **留客点**：[一个让观众看完的核心冲突/反转/干货]
📋 **内容大纲**：
  1. …
  2. …
  3. …
  4. …
📰 **事实依据**：[来自哪条新闻/发现，写清楚标题]

【硬性要求】
· 恰好输出 3 个选题，不多不少
· 所有选题必须有今日新闻的真实依据，禁止编造
· 选题标题要有冲突感/好奇心/利益点，不允许平淡描述性标题
· 输出以「## 🎬 今日自媒体选题」开头"""


def generate_topics(articles: list[dict], gaps: list[dict]) -> str:
    """调 R1 生成 3 个自媒体选题。失败返回空串，绝不抛异常。"""
    if not articles and not gaps:
        return ""

    try:
        system_prompt = _TOPICS_SYSTEM.format(platform=TOPIC_PLATFORM)
        user_prompt = _build_topics_user_prompt(articles, gaps)

        data = _call_deepseek_once(system_prompt, user_prompt,
                                   max_tokens=3000, model=TRIAGE_MODEL)
        content = data["choices"][0]["message"]["content"].strip()

        if not content:
            log.warning("⚠️ 自媒体选题生成返回空内容")
            return ""

        # 确保以标准标题开头
        if "## 🎬 今日自媒体选题" not in content:
            content = "## 🎬 今日自媒体选题\n\n" + content

        log.info(f"自媒体选题生成完成（{len(content)} 字）")
        return "\n\n" + content

    except Exception as e:
        log.warning(f"⚠️ 自媒体选题生成失败（{type(e).__name__}: {e}），已跳过")
        return ""
