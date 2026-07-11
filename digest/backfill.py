"""#3 参考源深度天花板回填。

对『高分 + reference 源 + fulltext 空』的条目，反查同题全文源回填正文，
让 Reuters/AP/Bloomberg 这类走 Google News 代理、原网页抓不到正文的重要新闻
也能拿到深度正文（来自另一家全文源），而不是永远只能 📡 快讯浅评。

降级铁律：任何失败 → 跳过该条，log.warning，继续；绝不抛异常、绝不中断主流程。
预算护栏：只针对 reference 条目，按分数挑、限数量；每条最多 1 次搜索 + 1 次抓取。
来源诚实（红线）：回填的正文来自另一家媒体，必须记 backfill_source，
                  下游注入 prompt 时如实标注，否则张冠李戴 = 幻觉。
"""

from __future__ import annotations

import logging

from .config import (
    BACKFILL_ENABLED,
    BACKFILL_MAX,
    BACKFILL_MIN_SCORE,
    FULLTEXT_MAX_CHARS,
    PREFERRED_FULLTEXT_DOMAINS,
)
from .fetch import fetch_one_fulltext
from .search_provider import web_search

log = logging.getLogger(__name__)


def backfill_reference_depth(articles: list[dict]) -> None:
    """对『高分 + reference 源 + fulltext 空』条目，搜同题全文源回填正文（原地改 article）。
    任何失败 → 跳过该条，绝不中断主流程。"""
    if not BACKFILL_ENABLED:
        return

    targets = [
        a
        for a in articles
        if a.get("reference")
        and not a.get("fulltext")
        and a.get("score", 0) >= BACKFILL_MIN_SCORE
    ][:BACKFILL_MAX]

    for a in targets:
        try:
            results = web_search(a["title"], num=5)
            for r in results:
                url = r["url"]
                from urllib.parse import urlparse
                host = urlparse(url).hostname or ""
                if any(host == d or host.endswith("." + d) for d in PREFERRED_FULLTEXT_DOMAINS):
                    text = fetch_one_fulltext(
                        {"link": url, "title": a["title"], "source": "backfill"}
                    )
                    if text:
                        a["fulltext"] = text[:FULLTEXT_MAX_CHARS]
                        # 兼容期同时写 backfill_source 和 backfill
                        a["backfill_source"] = url

                        import hashlib

                        norm_text = text.replace('\r\n', '\n').strip()
                        content_hash = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()

                        a["backfill"] = {
                            "url": url,
                            "canonical_url": r.get("url"), # Tavily/Exa 的 URL
                            "hostname": urlparse(url).hostname,
                            "publisher": r.get("author") or "Unknown",
                            "candidate_title": r.get("title", ""),
                            "search_rank": 1, # TODO: 如果有多个，可以填排序
                            "match_reason": "preferred_hostname+search_rank",
                            "captured_at": __import__('datetime').datetime.now(__import__('datetime').timezone(__import__('datetime').timedelta(hours=8))).isoformat(),
                            "content_sha256": content_hash,
                            "purpose": "context",
                            "fetched": True,
                        }

                        log.info(f"回填正文成功：{a['title']} ← {url}")
                        break
        except Exception as e:  # noqa: BLE001 — 降级铁律：吞掉一切，跳过该条
            log.warning(f"回填失败（跳过）：{a.get('title')} — {e}")
            continue
