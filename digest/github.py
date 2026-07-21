"""github.py — GitHub 热榜独立板块（每期 5 条：3 黑马 + 2 老牌）。

独立板块，与主新闻三大类（国际/科技/财经）完全隔离：
  GitHub Search API 拉候选 → 过滤去重集 → 选 3 黑马 / 2 老牌 → AI 批量写中文一句话
  → main.py 注入 🔥 GitHub 热榜板块。

与 bio.py 同构：独立成板块、容错优先、任何异常都不拖垮主推送。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import requests

from .ai import _call_deepseek_once
from .config import (
    TZ,
    GITHUB_ENABLED,
    GITHUB_MODEL,
    GITHUB_QUOTA,
    GITHUB_RISING_DAYS,
    GITHUB_VETERAN_DAYS,
    GITHUB_RISING_MIN_STARS,
    GITHUB_VETERAN_MIN_STARS,
    GITHUB_TIMEOUT,
    GITHUB_TOKEN,
)
from .storage import load_sent_github_repos

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  内部组件
# ═══════════════════════════════════════════════════


def _search_repos(query: str, per_page: int = 10) -> list[dict]:
    """调 GitHub Search API，失败 / 非 200 / 解析异常 → 返回 []（不抛）。

    API: GET https://api.github.com/search/repositories
    抽字段: full_name, html_url, description, stargazers_count, language
    """
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    params = {
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": per_page,
    }

    try:
        resp = requests.get(
            "https://api.github.com/search/repositories",
            headers=headers,
            params=params,
            timeout=GITHUB_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning(f"GitHub Search API 返回 {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f"GitHub Search API 请求/解析失败: {type(e).__name__}: {e}")
        return []

    items = data.get("items", [])
    repos = []
    for item in items:
        repos.append({
            "full_name": item.get("full_name", ""),
            "url": item.get("html_url", ""),
            "description": (item.get("description") or ""),
            "stargazers_count": item.get("stargazers_count", 0),
            "stars": item.get("stargazers_count", 0),  # 别名，兼容测试
            "language": item.get("language") or "",
        })
    return repos


def _today_str() -> str:
    """返回北京时间今天的 YYYY-MM-DD 字符串。"""
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _collect_candidates() -> tuple[list[dict], list[dict]]:
    """构造黑马/老牌查询，各拉一批候选。

    Returns:
        (rising_candidates, veteran_candidates) — 各自已标 kind 字段。
    """
    today = _today_str()

    # 黑马：created 近 N 天，最低星
    rising_since = (datetime.now(TZ) - timedelta(days=GITHUB_RISING_DAYS)).strftime("%Y-%m-%d")
    rising_query = f"created:>{rising_since} stars:>={GITHUB_RISING_MIN_STARS}"
    rising_raw = _search_repos(rising_query, per_page=GITHUB_QUOTA["rising"] * 2 + 3)
    for r in rising_raw:
        r["kind"] = "rising"

    # 老牌：pushed 近 N 天，高星。三级降级查询——从最严到最宽，确保每期都有候选。
    veteran_since = (datetime.now(TZ) - timedelta(days=GITHUB_VETERAN_DAYS)).strftime("%Y-%m-%d")
    veteran_raw: list[dict] = []
    for stars_floor in (GITHUB_VETERAN_MIN_STARS, 1000, 100):
        veteran_query = f"pushed:>{veteran_since} stars:>={stars_floor}"
        veteran_raw = _search_repos(veteran_query, per_page=GITHUB_QUOTA["veteran"] * 2 + 3)
        if veteran_raw:
            log.info(
                f"老牌查询 stars>=%d 命中 %d 條", stars_floor, len(veteran_raw)
            )
            break
        log.info(f"老牌查询 stars>=%d 无结果，降级重试", stars_floor)
    for r in veteran_raw:
        r["kind"] = "veteran"

    log.info(
        f"API 拉取候選：黑馬 {len(rising_raw)} 條、老牌 {len(veteran_raw)} 條"
    )
    return rising_raw, veteran_raw


def _select_five(
    rising: list[dict],
    veteran: list[dict],
    sent: set[str],
) -> list[dict]:
    """纯函数：从候选里挑 5 条（3 黑马 + 2 老牌，不够互补）。

    Args:
        rising: 黑马候选，已含 kind="rising"
        veteran: 老牌候选，已含 kind="veteran"
        sent: 已推送过的 full_name 集合（用于去重）

    Returns:
        最多 5 条 repo dict，按 stars 降序。
    """
    # 过滤已推送
    rising_fresh = [r for r in rising if r["full_name"] not in sent]
    veteran_fresh = [v for v in veteran if v["full_name"] not in sent]

    selected_rising = rising_fresh[:GITHUB_QUOTA["rising"]]
    selected_names = {r["full_name"] for r in selected_rising}

    # 老牌过滤已选黑马（防 full_name 重叠）
    veteran_filtered = [v for v in veteran_fresh if v["full_name"] not in selected_names]
    selected_veteran = veteran_filtered[:GITHUB_QUOTA["veteran"]]

    result = selected_rising + selected_veteran

    # 不够 5 条：用另一边剩余补满（黑马不足用老牌补，反之亦然）
    if len(result) < 5:
        deficit = 5 - len(result)
        used_names = {r["full_name"] for r in result}

        # 从所有候选里挑未使用的补位（维持 stars 降序）
        all_remaining = [r for r in (rising_fresh + veteran_fresh)
                         if r["full_name"] not in used_names]
        fillers = sorted(all_remaining, key=lambda x: x["stargazers_count"], reverse=True)[:deficit]
        # 补位的 kind 保持原值
        result.extend(fillers)

    # 按 stargazers_count 降序排列（便于阅读）
    result.sort(key=lambda x: x["stargazers_count"], reverse=True)
    return result


def _summarize_repos_zh(repos: list[dict]) -> list[dict]:
    """让 DeepSeek 批量写中文一句话。失败 → 回退用英文 description 截断。"""
    if not repos:
        return repos

    # 构造 prompt
    entries = []
    for i, r in enumerate(repos, 1):
        desc = r.get("description", "") or ""
        entries.append(
            f"{i}. [{r['full_name']}] ⭐{r['stargazers_count']} · {r.get('language', '无')}\n"
            f"   简介: {desc[:200]}"
        )
    repo_list = "\n".join(entries)

    system_prompt = (
        "你是一个技术编辑。下面有最多 5 个 GitHub 热门仓库，请你逐个用一句中文（≤30 字）简要概括："
        "『做什么 + 为什么火』。严格按照编号输出，每行一条，格式为「编号. 一句话」。"
        "不要任何前缀、总结或解释。"
    )
    user_prompt = f"GitHub 热门仓库列表：\n{repo_list}"

    try:
        data = _call_deepseek_once(
            system_prompt, user_prompt,
            max_tokens=400, model=GITHUB_MODEL,
        )
        content = data["choices"][0]["message"]["content"].strip()

        # 解析：按编号行拆
        lines = content.split("\n")
        parsed: dict[int, str] = {}
        for line in lines:
            m = re.match(r"^(\d+)[.、)\s]+\s*(.+)", line.strip())
            if m:
                idx = int(m.group(1))
                text = m.group(2).strip(' "“”')
                if text:
                    parsed[idx] = text

        # 写回
        for i, r in enumerate(repos, 1):
            if i in parsed and parsed[i]:
                r["description_zh"] = parsed[i]
            else:
                # 回退：截英文 description
                fallback = (r.get("description") or "")[:80]
                r["description_zh"] = fallback if fallback else "暂无简介"
                if i in parsed:
                    log.debug(f"  GitHub #{i} 解析为空，回退英文 description")
                else:
                    log.debug(f"  GitHub #{i} AI 未返回第 {i} 条，回退英文 description")

    except Exception as e:
        log.warning(f"GitHub AI 概括失败（{type(e).__name__}: {e}），全部回退英文 description")
        for r in repos:
            fallback = (r.get("description") or "")[:80]
            r["description_zh"] = fallback if fallback else "暂无简介"

    return repos


# ═══════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════


def pick_github_trending() -> list[dict] | None:
    """挑 5 个 trending 仓库（3 黑马 + 2 老牌，不够互补），附 AI 中文一句话。

    Returns:
        [{"full_name", "url", "description_zh", "stars", "language", "kind"}, ...]
        或 None（被关闭 / 无候选 / 任何异常）。
        kind ∈ {"rising", "veteran"}。
    """
    if not GITHUB_ENABLED:
        log.info("ℹ️ GitHub 热榜已关闭（GITHUB_ENABLED=False），跳过")
        return None

    try:
        rising, veteran = _collect_candidates()
        if not rising and not veteran:
            log.info("🔥 GitHub 热榜：API 无候选，跳过")
            return None

        sent = load_sent_github_repos()
        selected = _select_five(rising, veteran, sent)

        if not selected:
            log.info("🔥 GitHub 热榜：去重后无剩余，跳过")
            return None

        log.info(
            f"🔥 GitHub 热榜选出 {len(selected)} 条："
            + ", ".join(f"{r['full_name']}(⭐{r['stars']})" for r in selected)
        )

        selected = _summarize_repos_zh(selected)
        return selected

    except Exception as e:
        log.warning(f"⚠️ GitHub 热榜整体失败（{type(e).__name__}: {e}），已跳过")
        return None
