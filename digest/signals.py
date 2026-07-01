"""signals.py — 高价值信号监测（新产品、新工具、新玩法）。

独立板块，与主新闻隔离：
  signals.txt 指定源 → 并发抓 RSS → 按信号词打分 → 取 Top 3~5
  → AI 写一句话 → main.py 注入 📡 信号监测板块。

容错：任何异常 → 返回 None，绝不拖垮主推送。
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import feedparser

from .ai import _call_deepseek_once
from .fetch import _fetch_feed_content, clean_html
from .config import TZ, FEED_FETCH_WORKERS

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  配置（模块内，不污染 config.py）
# ═══════════════════════════════════════════════════

SIGNALS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "signals.txt",
)
SIGNALS_ENABLED = True
SIGNALS_MODEL = "deepseek-v4-flash"
SIGNALS_MAX_PER_FEED = 3
SIGNALS_TOP_N = 3              # 每期推送几条
SIGNALS_TIME_WINDOW_HOURS = 72  # newsletter 更新慢，放长窗口


# 信号词：标题命中即加分（新工具 / 发布 / 开源 / 突破）
SIGNAL_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"launch|release|new tool|open source|open-source|announce|introducing|"
    r"now available|beta|preview|shipping|published|"
    r"github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+|"  # GitHub 仓库链接
    r"benchmark|state.of.the.art|sota|breakthrough|"
    r"vibe coding|ai agent|claude code|codex|copilot|cursor|"
    r"free|free tier|openweight|open weight"
    r")\b",
    re.IGNORECASE,
)


def _load_signal_feeds() -> list[dict]:
    """从 signals.txt 解析信号源列表。

    Returns:
        [{"name": "...", "url": "..."}, ...]；文件不存在/解析失败 → []。
    """
    if not os.path.exists(SIGNALS_FILE):
        log.warning(f"signals.txt 不存在（{SIGNALS_FILE}），信号监测跳过")
        return []

    feeds = []
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|", 1)
                if len(parts) == 2:
                    name, url = parts[0].strip(), parts[1].strip()
                    if name and url:
                        feeds.append({"name": name, "url": url})
    except Exception as e:
        log.warning(f"解析 signals.txt 失败: {e}")
        return []

    log.info(f"📡 加载 {len(feeds)} 个信号源")
    return feeds


def _score_signal(title: str, summary: str) -> float:
    """给一条信号候选打分：信号词命中为主。"""
    text = f"{title} {summary}".lower()
    matches = SIGNAL_KEYWORDS_RE.findall(text)

    # 额外加分：GitHub 链接（直接指向新项目）
    score = len(matches) * 3.0
    if re.search(r"github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", text):
        score += 2.0
    return score


def _collect_signal_candidates() -> list[dict]:
    """并发抓所有 signal 源，打分，返回候选列表。"""
    feeds = _load_signal_feeds()
    if not feeds:
        return []

    now_utc = datetime.now(timezone.utc)
    candidates: list[dict] = []
    seen_titles: set[str] = set()

    contents: dict[int, bytes | None] = {}
    with ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
        future_map = {
            pool.submit(_fetch_feed_content, fi): idx
            for idx, fi in enumerate(feeds)
        }
        for fut in as_completed(future_map):
            contents[future_map[fut]] = fut.result()

    for idx, feed_info in enumerate(feeds):
        raw = contents.get(idx)
        if raw is None:
            log.debug(f"  signal [{feed_info['name']}] 下载失败，跳过")
            continue
        try:
            feed = feedparser.parse(raw)
        except Exception as e:
            log.debug(f"  signal [{feed_info['name']}] 解析失败: {e}")
            continue

        count = 0
        for entry in feed.entries:
            if count >= SIGNALS_MAX_PER_FEED:
                break
            title = entry.get("title", "").strip()
            if not title:
                continue
            title_lower = title.lower()
            if title_lower in seen_titles:
                continue

            summary = clean_html(entry.get("summary", entry.get("description", "")))
            link = entry.get("link", "").strip()

            score = _score_signal(title, summary)
            # 低分过滤：至少命中 1 个信号词才收录
            if score < 3.0:
                continue

            seen_titles.add(title_lower)
            candidates.append({
                "title": title,
                "summary": summary[:300],
                "url": link,
                "source": feed_info["name"],
                "score": score,
            })
            count += 1

    # 按分数降序
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:SIGNALS_TOP_N * 2]  # 留余量给 AI 精选


def _summarize_signals_zh(signals: list[dict]) -> list[dict]:
    """让 DeepSeek 写一句话概括。失败 → 回退原摘要截断。"""
    if not signals:
        return signals

    entries = []
    for i, s in enumerate(signals, 1):
        entries.append(
            f"{i}. [{s['source']}] {s['title']}\n"
            f"   摘要: {s['summary'][:200]}"
        )
    signal_list = "\n".join(entries)

    system_prompt = (
        "你是技术侦察员。下面有最多 6 条来自 AI/开发者社区的新信号（可能是新产品发布、新开源项目、新工具上线）。"
        "请逐个用一句中文（≤35 字）概括：『什么东西 + 为什么值得关注』。"
        "对不重要的条目输出「跳过」。严格按照编号输出，每行一条，格式为「编号. 一句话」。"
    )

    try:
        data = _call_deepseek_once(
            system_prompt, signal_list,
            max_tokens=400, model=SIGNALS_MODEL,
        )
        content = data["choices"][0]["message"]["content"].strip()

        parsed: dict[int, str] = {}
        for line in content.split("\n"):
            m = re.match(r"^(\d+)[.、)\s]+\s*(.+)", line.strip())
            if m:
                idx = int(m.group(1))
                text = m.group(2).strip(' "“”')
                if text and text != "跳过":
                    parsed[idx] = text

        # 只保留有 AI 概括的
        kept = []
        for i, s in enumerate(signals, 1):
            if i in parsed:
                s["summary_zh"] = parsed[i]
                kept.append(s)
            else:
                log.debug(f"  信号 #{i} 未生成概括或标记跳过，丢弃")
        return kept[:SIGNALS_TOP_N]

    except Exception as e:
        log.warning(f"信号 AI 概括失败（{type(e).__name__}: {e}），回退原摘要截断")
        for s in signals[:SIGNALS_TOP_N]:
            s["summary_zh"] = s["summary"][:80]
        return signals[:SIGNALS_TOP_N]


# ═══════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════


def pick_signals() -> list[dict] | None:
    """信号板块入口：挑 3 条高价值信号，附 AI 中文一句话。

    Returns:
        [{"title", "summary_zh", "url", "source"}, ...]
        或 None（无候选 / 被关闭 / 任何异常）。
    """
    if not SIGNALS_ENABLED:
        return None

    try:
        candidates = _collect_signal_candidates()
        if not candidates:
            log.info("📡 信号监测：无高价值信号")
            return None

        log.info(f"📡 信号监测候选 {len(candidates)} 条，送入 AI 精选")
        selected = _summarize_signals_zh(candidates)

        if not selected:
            log.info("📡 信号监测：AI 精选后无剩余")
            return None

        for s in selected:
            log.info(f"  📡 [{s['source']}] {s['title'][:50]} → {s.get('summary_zh', '')[:50]}")

        return selected

    except Exception as e:
        log.warning(f"⚠️ 信号监测整体失败（{type(e).__name__}: {e}），已跳过")
        return None
