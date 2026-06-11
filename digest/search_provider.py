"""搜索源统一抽象层：对外只暴露 web_search()，内部自动 Exa→Tavily 降级。

MVP 注意：月调用计数仅在进程内累计（重启归零），不持久化到文件/Actions cache。
持久化跨次计数是待加固项——当前靠「进程内硬上限 + API 报错触发降级」兜底。
"""

from __future__ import annotations

import logging

import requests

from . import config  # 通过 config 模块引用，方便测试时 monkeypatch

log = logging.getLogger(__name__)

# 进程内搜索调用计数（Exa + Tavily 合计），防烧钱死循环
_call_counter: int = 0


def _reset_counter() -> None:
    """仅供测试使用：重置进程内计数器。"""
    global _call_counter
    _call_counter = 0


def _normalize_exa(result: dict) -> dict:
    """把 Exa 单条结果归一化为统一格式。"""
    # TODO: 首次真实调用时核对 Exa 官方字段（当前按 docs.exa.ai 2024 版本）
    return {
        "title":     result.get("title", ""),
        "url":       result.get("url", ""),
        "snippet":   result.get("text", ""),       # contents.text 返回在 text 字段
        "published": result.get("publishedDate", ""),
        "source":    "exa",
    }


def _normalize_tavily(result: dict) -> dict:
    """把 Tavily 单条结果归一化为统一格式。"""
    # TODO: 首次真实调用时核对 Tavily 官方字段（当前按 tavily.com/docs 2024 版本）
    return {
        "title":     result.get("title", ""),
        "url":       result.get("url", ""),
        "snippet":   result.get("content", ""),
        "published": result.get("published_date", ""),
        "source":    "tavily",
    }


def _search_exa(query: str, num: int) -> list[dict]:
    """调 Exa API，返回归一化列表；失败抛异常（由调用方捕获决定降级）。"""
    resp = requests.post(
        "https://api.exa.ai/search",
        headers={
            "x-api-key": config.EXA_API_KEY or "",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "type": "auto",
            "numResults": num,
            "contents": {"text": {"maxCharacters": 500}},
        },
        timeout=20,
    )
    resp.raise_for_status()  # 402/429 → HTTPError，调用方捕获后降级
    data = resp.json()
    results = data.get("results", [])
    return [_normalize_exa(r) for r in results]


def _search_tavily(query: str, num: int) -> list[dict]:
    """调 Tavily API，返回归一化列表；失败抛异常（由调用方捕获返回 []）。"""
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": config.TAVILY_API_KEY or "",
            "query": query,
            "max_results": num,
            "search_depth": "basic",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    return [_normalize_tavily(r) for r in results]


def web_search(query: str, num: int = 8) -> list[dict]:
    """统一搜索接口。返回 [{title, url, snippet, published, source}, ...]。

    主用 Exa；额度耗尽(402/429)或异常 → 自动降级 Tavily；都失败 → 返回 []。
    任何情况下绝不抛异常，侦察兵调用方可放心使用。
    """
    global _call_counter

    # 月调用上限检查（进程内）
    if _call_counter >= config.SEARCH_MONTHLY_CAP:
        log.warning(
            f"⛔ 搜索月调用已达上限 {config.SEARCH_MONTHLY_CAP}，跳过本次搜索（query={query!r}）"
        )
        return []

    exa_key = config.EXA_API_KEY
    tavily_key = config.TAVILY_API_KEY

    # ── 尝试 Exa ──────────────────────────────────
    if exa_key:
        try:
            _call_counter += 1
            results = _search_exa(query, num)
            log.info(f"🔍 Exa 搜索完成：query={query!r}，共 {len(results)} 条")
            return results
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (402, 429):
                log.warning(f"⚠️ Exa 不可用（HTTP {status}），已降级 Tavily")
            else:
                log.warning(f"⚠️ Exa HTTP 错误（{status}），已降级 Tavily")
        except Exception as e:
            log.warning(f"⚠️ Exa 不可用（{type(e).__name__}），已降级 Tavily")

    # ── 尝试 Tavily ──────────────────────────────
    if tavily_key:
        try:
            _call_counter += 1
            results = _search_tavily(query, num)
            log.info(f"🔍 Tavily 搜索完成：query={query!r}，共 {len(results)} 条")
            return results
        except Exception as e:
            log.warning(f"⚠️ Tavily 搜索失败（{type(e).__name__}: {e}），返回空列表")
            return []

    # ── 两个 key 都没配 ──────────────────────────
    if not exa_key and not tavily_key:
        log.info("ℹ️ EXA_API_KEY 和 TAVILY_API_KEY 均未配置，搜索跳过")

    return []
