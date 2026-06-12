"""scout.py — 信息差侦察兵 Agent（手写 ReAct 循环）。

LLM(大脑) + web_search(Exa→Tavily) + read_url(trafilatura) + ReAct(loop)
→ 最多 SCOUT_FINDINGS 条「低曝光高价值」发现，每条带：是什么/为何有价值/为何被低估/链接。

护栏（防烧钱/死循环）：
  · SCOUT_MAX_ROUNDS：最多循环轮数
  · SCOUT_MAX_TOOL_CALLS：工具调用总次数
  · SCOUT_TIMEOUT_S：总超时秒数（默认 180s）
  · JSON 解析失败 / 工具连续失败 → 优雅退出返回已有发现
  · SCOUT_ENABLED=False → 静默跳过
任何异常 → 返回 []，绝不拖垮主简报推送。
"""

from __future__ import annotations

import json
import logging
import re
import time

from .ai import _call_deepseek_once
from .config import (
    SCOUT_ENABLED,
    SCOUT_FINDINGS,
    SCOUT_MAX_ROUNDS,
    SCOUT_MAX_TOOL_CALLS,
    SCOUT_MODEL,
)
from .fetch import fetch_one_fulltext
from .search_provider import web_search

log = logging.getLogger(__name__)

SCOUT_TIMEOUT_S = 180  # 侦察兵单次运行总超时（秒）
_TOOL_FAIL_LIMIT = 3   # 工具连续失败超过此数 → 提前退出


# ──────────────────────────── 工具层 ────────────────────────────

def _tool_search(query: str) -> str:
    """调 web_search，把结果格式化成 LLM 可读的文本。"""
    results = web_search(query, num=6)
    if not results:
        return "【搜索结果为空，请尝试其他查询词或直接 finish】"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. 标题：{r['title']}\n"
            f"   URL：{r['url']}\n"
            f"   摘要：{r['snippet'][:200]}\n"
            f"   发布：{r['published']}"
        )
    return "\n\n".join(lines)


def _tool_read(url: str) -> str:
    """用 trafilatura 抓 url 正文，返回前 800 字。"""
    art = {"link": url, "title": "", "source": "scout"}
    text = fetch_one_fulltext(art)
    if not text:
        return f"【无法抓取正文：{url}】"
    return text[:800]


# ──────────────────────────── ReAct 循环 ────────────────────────────

_SYSTEM_PROMPT = """你是一位信息差侦察兵，任务：为一位「硬核 PC 游戏/MOD 玩家 + 正在学 AI 编程 + 身处中国」的读者，主动搜索并挖掘以下类型内容：
· 主流大媒体还没放大、但有认知增量的技术/产业新动态
· 藏在小众博客/论文/论坛/一手公告里、值得关注的低曝光发现

【可用工具】每轮必须输出一个纯 JSON 动作（不要有任何其他文字）：
搜索：{"thought":"…","action":"search","args":{"query":"…"}}
读取：{"thought":"…","action":"read","args":{"url":"…"}}
完成：{"thought":"…","action":"finish","args":{"findings":[{"title":"…","why_valuable":"…","why_underreported":"…","url":"…"}]}}

【完成条件】发现至少 1 条（最多 NNN 条）有价值内容后输出 finish。若搜索无果也请 finish（findings 为空列表）。
【禁止幻觉】url 必须是搜索/读取中真实出现的链接，不要编造。"""


def _parse_action(text: str) -> dict | None:
    """从 LLM 输出中抠 JSON 动作，失败返回 None。"""
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def scout_for_gaps() -> list[dict]:
    """侦察兵入口。返回 [{title, why_valuable, why_underreported, url}, ...]，最多 SCOUT_FINDINGS 条。
    任何失败 → 返回 []，绝不抛异常。
    """
    if not SCOUT_ENABLED:
        log.info("ℹ️ 侦察兵已关闭（SCOUT_ENABLED=False），跳过")
        return []

    try:
        return _run_scout()
    except Exception as e:
        log.warning(f"⚠️ 侦察兵整体失败（{type(e).__name__}: {e}），已跳过")
        return []


def _run_scout() -> list[dict]:
    """内部 ReAct 循环，异常向上传递给 scout_for_gaps 捕获。"""
    start_ts = time.time()
    system_prompt = _SYSTEM_PROMPT.replace("NNN", str(SCOUT_FINDINGS))

    # 对话历史：拍平成 user 消息的累积块，每轮追加 assistant 动作 + observation
    history_lines: list[str] = []
    history_lines.append("请开始侦察，逐步搜索并发现有价值的低曝光内容。")

    tool_call_count = 0
    consecutive_tool_fails = 0
    findings: list[dict] = []

    for round_i in range(1, SCOUT_MAX_ROUNDS + 1):
        # 超时检查
        if time.time() - start_ts > SCOUT_TIMEOUT_S:
            log.warning(f"⏱️ 侦察兵超时（>{SCOUT_TIMEOUT_S}s），用已有 {len(findings)} 条结果收尾")
            break

        user_prompt = "\n\n".join(history_lines)

        try:
            data = _call_deepseek_once(system_prompt, user_prompt,
                                       max_tokens=1000, model=SCOUT_MODEL)
            llm_output = data["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning(f"侦察兵第 {round_i} 轮 LLM 调用失败：{e}，提前退出")
            break

        history_lines.append(f"[助手动作 Round {round_i}]\n{llm_output}")

        action = _parse_action(llm_output)
        if action is None:
            log.warning(f"侦察兵第 {round_i} 轮 JSON 解析失败，提前退出")
            break

        act_type = action.get("action", "")
        args = action.get("args", {})
        thought = action.get("thought", "")
        log.info(f"侦察兵 Round {round_i}: action={act_type}  thought={thought[:60]}")

        # ── finish ──────────────────────────────────
        if act_type == "finish":
            raw_findings = args.get("findings", [])
            for f in raw_findings:
                if isinstance(f, dict) and f.get("url") and f.get("title"):
                    findings.append({
                        "title":            str(f.get("title", "")),
                        "why_valuable":     str(f.get("why_valuable", "")),
                        "why_underreported": str(f.get("why_underreported", "")),
                        "url":              str(f.get("url", "")),
                    })
            log.info(f"侦察兵完成，共发现 {len(findings)} 条")
            return findings[:SCOUT_FINDINGS]

        # ── search ──────────────────────────────────
        elif act_type == "search":
            if tool_call_count >= SCOUT_MAX_TOOL_CALLS:
                log.warning("侦察兵工具调用次数达上限，强制 finish")
                break
            query = args.get("query", "")
            tool_call_count += 1
            observation = _tool_search(query)
            if observation.startswith("【搜索结果为空"):
                consecutive_tool_fails += 1
            else:
                consecutive_tool_fails = 0
            history_lines.append(f"[搜索结果 query={query!r}]\n{observation}")

        # ── read ────────────────────────────────────
        elif act_type == "read":
            if tool_call_count >= SCOUT_MAX_TOOL_CALLS:
                log.warning("侦察兵工具调用次数达上限，强制 finish")
                break
            url = args.get("url", "")
            tool_call_count += 1
            observation = _tool_read(url)
            if observation.startswith("【无法抓取"):
                consecutive_tool_fails += 1
            else:
                consecutive_tool_fails = 0
            history_lines.append(f"[页面正文 url={url!r}]\n{observation}")

        else:
            log.warning(f"侦察兵第 {round_i} 轮未知 action={act_type!r}，跳过")
            consecutive_tool_fails += 1

        # 连续工具失败上限
        if consecutive_tool_fails >= _TOOL_FAIL_LIMIT:
            log.warning(f"侦察兵连续工具失败 {consecutive_tool_fails} 次，提前退出")
            break

    return findings[:SCOUT_FINDINGS]
