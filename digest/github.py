"""github.py — GitHub 热榜独立板块（每期 5 条：3 黑马 + 2 老牌）。

独立板块，与主新闻三大类（国际/科技/财经）完全隔离：
  调 GitHub Search API → 选 3 黑马 + 2 老牌 → AI 批量写中文一句话
  → main.py 注入 🔥 GitHub 热榜 板块。

为什么单开模块而非塞进主打分：
  · 数据源不同（GitHub API，非 RSS）；
  · 去重维度不同（repo full_name，非文章 URL）；
  · 与 bio 同构：独立成板块、容错优先、任何异常都不拖垮主推送。

容错：任何异常 → 返回 None，绝不抛给主流水线。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import requests

from .ai import _call_deepseek_once
from .config import (
    GITHUB_ENABLED,
    GITHUB_MODEL,
    GITHUB_QUOTA,
    GITHUB_RISING_DAYS,
    GITHUB_VETERAN_DAYS,
    GITHUB_RISING_MIN_STARS,
    GITHUB_VETERAN_MIN_STARS,
    GITHUB_TIMEOUT,
    GITHUB_TOKEN,
    TZ,
)
from .storage import load_sent_github_repos

log = logging.getLogger(__name__)

# GitHub Search API 端点
_SEARCH_URL = "https://api.github.com/search/repositories"


# ═══════════════════════════════════════════════════
#  4.1 _search_repos — 调 GitHub Search API
# ═══════════════════════════════════════════════════

def _search_repos(query: str, per_page: int = 15) -> list[dict]:
    """调 GitHub Search API，返回 repo 列表。

    Header: Accept: application/vnd.github+json；有 GITHUB_TOKEN 则加 Authorization。
    超时 GITHUB_TIMEOUT。失败 / 非 200 / 解析异常 → 返回 []（不抛）。
    抽字段：full_name、html_url、description、stargazers_count、language。
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
        resp = requests.get(_SEARCH_URL, headers=headers, params=params,
                            timeout=GITHUB_TIMEOUT)
        if resp.status_code != 200:
            log.warning(f"GitHub Search API 返回 {resp.status_code}：{resp.text[:200]}")
            return []
        data = resp.json()
    except Exception as e:
        log.warning(f"GitHub Search API 请求/解析失败（{type(e).__name__}: {e}）")
        return []

    items = data.get("items", [])
    results: list[dict] = []
    for item in items:
        results.append({
            "full_name": item.get("full_name", ""),
            "html_url": item.get("html_url", ""),
            "description": (item.get("description") or ""),
            "stargazers_count": item.get("stargazers_count", 0),
            "language": (item.get("language") or ""),
        })
    return results


# ═══════════════════════════════════════════════════
#  4.2 _collect_candidates — 收集黑马 + 老牌候选
# ═══════════════════════════════════════════════════

def _collect_candidates() -> tuple[list[dict], list[dict]]:
    """构造黑马/老牌查询，各取候选，返回 (rising, veteran)。

    黑马：created 近 GITHUB_RISING_DAYS 天，stars >= GITHUB_RISING_MIN_STARS。
    老牌：pushed 近 GITHUB_VETERAN_DAYS 天，stars >= GITHUB_VETERAN_MIN_STARS。
    日期用北京时间 TZ 推算，格式 YYYY-MM-DD。
    各取 per_page = quota×2 + 余量，给去重过滤留候选。
    """
    now = datetime.now(TZ)
    rising_date = (now - timedelta(days=GITHUB_RISING_DAYS)).strftime("%Y-%m-%d")
    veteran_date = (now - timedelta(days=GITHUB_VETERAN_DAYS)).strftime("%Y-%m-%d")

    # 黑马查询：近 N 天新建 + 最低星数
    rising_query = f"created:>{rising_date} stars:>={GITHUB_RISING_MIN_STARS}"
    rising_per_page = GITHUB_QUOTA["rising"] * 2 + 5  # 3*2+5=11，够过滤
    rising_raw = _search_repos(rising_query, per_page=rising_per_page)
    rising = []
    for r in rising_raw:
        r["kind"] = "rising"
        rising.append(r)
    log.info(f"GitHub 黑马候选：API 返回 {len(rising)} 条（query: {rising_query}）")

    # 老牌查询：近 N 天有更新 + 高星
    veteran_query = f"pushed:>{veteran_date} stars:>={GITHUB_VETERAN_MIN_STARS}"
    veteran_per_page = GITHUB_QUOTA["veteran"] * 2 + 5  # 2*2+5=9，够过滤
    veteran_raw = _search_repos(veteran_query, per_page=veteran_per_page)
    veteran = []
    for r in veteran_raw:
        r["kind"] = "veteran"
        veteran.append(r)
    log.info(f"GitHub 老牌候选：API 返回 {len(veteran)} 条（query: {veteran_query}）")

    return rising, veteran


# ═══════════════════════════════════════════════════
#  4.3 _select_five — 纯函数挑选 5 个 repo
# ═══════════════════════════════════════════════════

def _select_five(rising: list[dict], veteran: list[dict],
                 sent: set[str]) -> list[dict]:
    """从黑马+老牌候选里挑 5 个（3 黑马 + 2 老牌，不够互补满 5）。

    纯函数（无 IO），便于单测。

    逻辑：
    1. 过滤 sent 里的 full_name
    2. 取前 QUOTA["rising"] 个黑马（按 stars 降序，API 已排）
    3. 老牌过滤 sent + 已选黑马 full_name，取前 QUOTA["veteran"] 个
    4. 总数 < 5：用另一边剩余补满
    5. 上限 5；两边合计 < 5 返回实际数
    """
    # 过滤去重集
    rising_avail = [r for r in rising if r["full_name"] not in sent]
    veteran_avail = [v for v in veteran if v["full_name"] not in sent]

    quota_rising = GITHUB_QUOTA["rising"]
    quota_veteran = GITHUB_QUOTA["veteran"]

    # 选黑马
    selected_rising = rising_avail[:quota_rising]
    rising_names = {r["full_name"] for r in selected_rising}

    # 选老牌（排除已选黑马 + sent）
    veteran_filtered = [v for v in veteran_avail
                        if v["full_name"] not in rising_names]
    selected_veteran = veteran_filtered[:quota_veteran]

    selected = selected_rising + selected_veteran

    # 不足 5：用另一边剩余补满
    if len(selected) < sum(GITHUB_QUOTA.values()):
        used_names = {r["full_name"] for r in selected}
        # 剩余黑马
        remaining_rising = [r for r in rising_avail if r["full_name"] not in used_names]
        # 剩余老牌
        remaining_veteran = [v for v in veteran_avail if v["full_name"] not in used_names]
        # 合并剩余，按 stars 降序
        pool = sorted(remaining_rising + remaining_veteran,
                      key=lambda x: x["stargazers_count"], reverse=True)
        needed = sum(GITHUB_QUOTA.values()) - len(selected)
        selected.extend(pool[:needed])

    return selected[:sum(GITHUB_QUOTA.values())]


# ═══════════════════════════════════════════════════
#  4.4 _summarize_repos_zh — AI 批量写中文一句话
# ═══════════════════════════════════════════════════

def _summarize_repos_zh(repos: list[dict]) -> list[dict]:
    """5 个 repo 拼一个 prompt，一次 DeepSeek 调用写中文一句话。

    失败 / 解析不齐 → 该 repo 回退用原始英文 description 截断（≤80 字）。
    """
    if not repos:
        return repos

    # 构建 prompt
    repo_lines = []
    for i, r in enumerate(repos, 1):
        desc = r.get("description", "") or ""
        repo_lines.append(
            f"{i}. {r['full_name']}（⭐{r['stargazers_count']}，{r.get('language', '?')}）\n"
            f"   英文描述：{desc[:200]}"
        )
    repo_text = "\n".join(repo_lines)

    system_prompt = (
        "你是技术编辑。下面有 5 个 GitHub 仓库，请逐个用一句中文概括（≤30 字），"
        "点明「做什么 + 为啥火」。严格按编号输出，每行一条：\n"
        "1. 中文一句话\n2. 中文一句话\n...\n"
        "只输出这 5 行，不要任何前缀、解释或额外文字。"
    )
    user_prompt = f"请为以下 GitHub 仓库写中文一句话概括：\n\n{repo_text}"

    # 默认回退：英文 description 截断
    fallback_lines: list[str] = []
    for r in repos:
        desc = r.get("description", "") or ""
        fallback_lines.append(desc[:80])

    try:
        data = _call_deepseek_once(system_prompt, user_prompt,
                                   max_tokens=500, model=GITHUB_MODEL)
        content = data["choices"][0]["message"]["content"].strip()
        # 解析：按行取，跳过空行和编号前缀
        lines = [l.strip() for l in content.split("\n") if l.strip()]
        parsed: list[str] = []
        for line in lines:
            # 去掉可能的编号前缀 "1. " "1) " 等
            cleaned = re.sub(r'^\d+[\.\)、\s]+', '', line).strip(' "“”')
            if cleaned:
                parsed.append(cleaned)

        # 按编号写回
        for i, r in enumerate(repos):
            if i < len(parsed) and parsed[i]:
                r["description_zh"] = parsed[i]
            else:
                r["description_zh"] = fallback_lines[i]
    except Exception as e:
        log.warning(f"GitHub AI 概括失败（{type(e).__name__}: {e}），回退英文 description")
        for i, r in enumerate(repos):
            r["description_zh"] = fallback_lines[i]

    return repos


# ═══════════════════════════════════════════════════
#  4.5 pick_github_trending — 入口
# ═══════════════════════════════════════════════════

def pick_github_trending() -> list[dict] | None:
    """挑 5 个 trending 仓库（3 黑马 + 2 老牌，不够互补），附 AI 中文一句话。

    返回 [{full_name, url, description_zh, stars, language, kind}, ...]
    或 None（被关闭 / 无候选 / 任何异常）。kind ∈ {"rising", "veteran"}。
    """
    if not GITHUB_ENABLED:
        log.info("ℹ️ GitHub 热榜板块已关闭（GITHUB_ENABLED=False），跳过")
        return None

    try:
        # 收集候选
        rising, veteran = _collect_candidates()
        if not rising and not veteran:
            log.info("🔥 GitHub 热榜：API 均无候选，跳过")
            return None

        # 加载去重集
        sent = load_sent_github_repos()
        log.info(f"GitHub 去重集：{len(sent)} 条已推送 repo")

        # 挑选
        selected = _select_five(rising, veteran, sent)
        if not selected:
            log.info("🔥 GitHub 热榜：去重后无剩余候选，跳过")
            return None

        log.info(f"🔥 GitHub 热榜选中 {len(selected)} 条："
                 f"{', '.join(r['full_name'] for r in selected)}")

        # AI 概括
        selected = _summarize_repos_zh(selected)

        # 统一输出字段名
        result = []
        for r in selected:
            result.append({
                "full_name": r["full_name"],
                "url": r["html_url"],
                "description_zh": r.get("description_zh", r.get("description", "")[:80]),
                "stars": r["stargazers_count"],
                "language": r.get("language", ""),
                "kind": r.get("kind", "rising"),
            })
        return result

    except Exception as e:
        log.warning(f"⚠️ GitHub 热榜板块整体失败（{type(e).__name__}: {e}），已跳过")
        return None
