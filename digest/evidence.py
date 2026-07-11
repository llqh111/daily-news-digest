"""P2-A 证据优先的内容质量增强 - 证据抽取与校验。"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from .config import (
    EVIDENCE_CARDS_ENABLED,
    EVIDENCE_MAX_FACTS,
    EVIDENCE_MIN_ANCHORS,
    EVIDENCE_TEXT_MAX_CHARS,
    TZ,
)
from .scoring import extract_proper_nouns
from .factcheck import extract_numerical_claims

log = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    """规范化：仅统一换行和连续空白，不改写文字。"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def normalize_url(url: str) -> str:
    """URL 规范化算法：
    scheme/hostname 小写；移除 fragment；移除默认端口；
    路径合并重复 / 并移除非根路径尾部 /；
    query 参数排序；删除跟踪参数。
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()

        # 移除默认端口
        if scheme == "http" and netloc.endswith(":80"):
            netloc = netloc[:-3]
        elif scheme == "https" and netloc.endswith(":443"):
            netloc = netloc[:-4]

        # 路径合并重复 /
        path = re.sub(r'/+', '/', parsed.path)
        # 移除非根路径尾部 /
        if len(path) > 1 and path.endswith('/'):
            path = path[:-1]

        # query 参数
        qs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered_qs = []
        for k, v in qs:
            kl = k.lower()
            if kl.startswith("utm_") or kl in ("fbclid", "gclid", "oc"):
                continue
            filtered_qs.append((k, v))
        filtered_qs.sort(key=lambda x: x[0])
        query = urlencode(filtered_qs)

        return urlunparse((scheme, netloc, path, parsed.params, query, ""))
    except Exception:
        return url


def is_google_news_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.hostname in ("news.google.com", "news.google.co.uk") and parsed.path.startswith("/rss/articles/")


def build_article_id(article: dict, session_logical_date: str = "") -> str:
    """按 id_scheme_version=1 生成稳定 ID。"""
    identity_key = ""

    canonical_url = article.get("canonical_url")
    if canonical_url:
        identity_key = f"url:{canonical_url}"
    else:
        url = article.get("link") or ""
        norm_url = normalize_url(url)
        if norm_url and not is_google_news_url(url):
            identity_key = f"url:{norm_url}"
        else:
            # Fallback
            source = article.get("source", "").lower().strip()
            title = article.get("title", "").lower().strip()
            date_val = article.get("published") or session_logical_date
            identity_key = f"fallback:{source}|{title}|{date_val}"

    hash_val = _sha256(identity_key)[:24]
    return f"a1_{hash_val}"


def _get_captured_at() -> str:
    return datetime.now(TZ).isoformat()


def build_evidence_card(article: dict, session_logical_date: str = "") -> dict:
    """从单条 article 构造 EvidenceCard；失败时返回最小可用卡片。"""
    article_id = build_article_id(article, session_logical_date)

    card = {
        "version": 1,
        "id_scheme_version": 1,
        "article_id": article_id,
        "headline": article.get("title", ""),
        "confirmed_facts": [],
        "entities": [],
        "numbers": [],
        "dates": [],
        "unknowns": [],
        "sources": [],
        "coverage": {
            "has_fulltext": False,
            "has_multiple_sources": False,
            "fact_count": 0,
            "anchor_count": 0
        }
    }

    try:
        # --- 1. Sources ---
        primary_source = {
            "id": "source:primary",
            "role": "primary",
            "publisher": article.get("source", ""),
            "url": article.get("link", ""),
            "canonical_url": article.get("canonical_url"),
            "hostname": urlparse(article.get("link", "")).hostname if article.get("link") else "",
            "content_level": "headline",
            "trust_tier": "unknown",  # 默认兜底，第一版可根据配置填充
            "captured_at": _get_captured_at(),
            "content_sha256": ""
        }

        # 判断 trust_tier
        from .config import SOURCE_TRUST
        trust_score = SOURCE_TRUST.get(primary_source["publisher"], 0)
        if trust_score >= 2:
            primary_source["trust_tier"] = "major_media"
        elif trust_score == 1:
            primary_source["trust_tier"] = "secondary"

        fulltext = article.get("fulltext", "")
        summary = article.get("summary", "")
        title = article.get("title", "")

        # 确定 primary 内容级别
        if article.get("backfill_source") or article.get("backfill"):
            # 如果是回填的，说明 primary 本身只有 summary/headline
            if summary:
                primary_source["content_level"] = "summary"
                primary_content = f"{title}\n{summary}"
            else:
                primary_source["content_level"] = "headline"
                primary_content = title
        else:
            if fulltext:
                primary_source["content_level"] = "fulltext"
                primary_content = fulltext
            elif summary:
                primary_source["content_level"] = "summary"
                primary_content = f"{title}\n{summary}"
            else:
                primary_source["content_level"] = "headline"
                primary_content = title

        primary_source["content_sha256"] = _sha256(_normalize_text(primary_content))
        card["sources"].append(primary_source)

        # Context (backfill)
        backfill_info = article.get("backfill")
        if not backfill_info and article.get("backfill_source"):
            # 兼容老 backfill_source 字段
            backfill_info = {
                "url": article.get("backfill_source"),
                "hostname": urlparse(article.get("backfill_source")).hostname,
                "publisher": "Unknown",
                "content_sha256": _sha256(_normalize_text(fulltext)) if fulltext else ""
            }

        if backfill_info:
            context_source = {
                "id": "source:backfill",
                "role": "context",
                "publisher": backfill_info.get("publisher", ""),
                "url": backfill_info.get("url", ""),
                "canonical_url": backfill_info.get("canonical_url"),
                "hostname": backfill_info.get("hostname", ""),
                "content_level": "fulltext",
                "trust_tier": "major_media", # 假设 backfill 的都比较权威
                "captured_at": backfill_info.get("captured_at", _get_captured_at()),
                "content_sha256": backfill_info.get("content_sha256", _sha256(_normalize_text(fulltext)))
            }
            card["sources"].append(context_source)

        # Corroboration (cluster_titles)
        cluster_titles = article.get("cluster_titles", [])
        for i, ct in enumerate(cluster_titles):
            # 第一版仅作标题佐证
            card["sources"].append({
                "id": f"source:corroboration:{i}",
                "role": "corroboration",
                "publisher": "Unknown",
                "url": "",
                "canonical_url": None,
                "hostname": "",
                "content_level": "headline",
                "trust_tier": "unknown",
                "captured_at": _get_captured_at(),
                "content_sha256": _sha256(_normalize_text(ct))
            })

        # --- 2. Entities, Numbers, Dates ---
        search_text = fulltext if fulltext else (summary if summary else title)
        search_text_norm = _normalize_text(search_text)

        # 实体抽取
        entities = extract_proper_nouns(search_text)
        card["entities"] = list(set(entities))

        # 数字和日期抽取
        claims = extract_numerical_claims(search_text)
        numbers = []
        dates = []
        for c in claims:
            if c["type"] == "日期":
                dates.append(c["claim"])
            else:
                numbers.append(c["claim"])

        card["numbers"] = list(set(numbers))
        card["dates"] = list(set(dates))

        # --- 3. Confirmed Facts ---
        # 简单的切句逻辑
        sentences = re.split(r'(?<=[。！？.!?])\s+', search_text_norm)
        fact_id_counter = 1

        for sent in sentences:
            if len(card["confirmed_facts"]) >= EVIDENCE_MAX_FACTS:
                break
            if not sent.strip():
                continue

            # 判断句子中是否包含 anchor（实体、数字、日期）
            anchors_in_sent = []
            for e in card["entities"]:
                if e in sent: anchors_in_sent.append(e)
            for n in card["numbers"]:
                if n in sent: anchors_in_sent.append(n)
            for d in card["dates"]:
                if d in sent: anchors_in_sent.append(d)

            if len(anchors_in_sent) >= EVIDENCE_MIN_ANCHORS:
                excerpt = sent[:280]
                norm_excerpt = _normalize_text(excerpt)

                # 绑定对应的 source
                # 简单处理：如果有 backfill，正文来自 backfill
                target_source_id = "source:primary"
                target_sha = primary_source["content_sha256"]
                if backfill_info and fulltext and sent in _normalize_text(fulltext):
                    target_source_id = "source:backfill"
                    target_sha = backfill_info.get("content_sha256", "")

                fact = {
                    "fact_id": f"f{fact_id_counter}",
                    "text": excerpt, # 第一版直接陈述事实
                    "source_id": target_source_id,
                    "captured_at": _get_captured_at(),
                    "content_sha256": target_sha,
                    "excerpt": excerpt,
                    "sentence_hash": _sha256(norm_excerpt),
                    "anchors": list(set(anchors_in_sent))
                }
                card["confirmed_facts"].append(fact)
                fact_id_counter += 1

        # --- 4. Unknowns ---
        if not fulltext:
            card["unknowns"].append("正文未抓到")
        if not dates:
            card["unknowns"].append("缺少明确时间")

        # --- 5. Coverage ---
        card["coverage"]["has_fulltext"] = bool(fulltext)
        card["coverage"]["has_multiple_sources"] = len(card["sources"]) > 1
        card["coverage"]["fact_count"] = len(card["confirmed_facts"])
        card["coverage"]["anchor_count"] = len(card["entities"]) + len(card["numbers"]) + len(card["dates"])

    except Exception as e:
        log.warning(f"构建 EvidenceCard 时发生异常: {e}，返回最小卡片")
        # 如果发生异常，返回初始化的 card (已包含必要的最少字段)
        pass

    return card


def build_evidence_cards(articles: list[dict], session_logical_date: str = "") -> None:
    """原地为每条 article 写入 evidence_card；单条失败不影响其他条目。"""
    if not EVIDENCE_CARDS_ENABLED:
        return

    if not session_logical_date:
        session_logical_date = datetime.now(TZ).strftime("%Y-%m-%d")

    for article in articles:
        try:
            card = build_evidence_card(article, session_logical_date)
            article["evidence_card"] = card
            article["article_id"] = card["article_id"]
        except Exception as e:
            log.warning(f"为条目 {article.get('title')} 生成 evidence_card 失败: {e}")


def validate_evidence_card(card: dict) -> list[str]:
    """返回结构问题列表；空列表表示结构有效。"""
    issues = []
    if not isinstance(card, dict):
        return ["card must be a dict"]

    if card.get("version") != 1:
        issues.append("version must be 1")
    if not card.get("article_id"):
        issues.append("missing article_id")

    for fact in card.get("confirmed_facts", []):
        if not fact.get("excerpt"):
            issues.append(f"fact {fact.get('fact_id')} missing excerpt")
        if not fact.get("content_sha256"):
            issues.append(f"fact {fact.get('fact_id')} missing content_sha256")
        if not fact.get("sentence_hash"):
            issues.append(f"fact {fact.get('fact_id')} missing sentence_hash")
        if not fact.get("source_id"):
            issues.append(f"fact {fact.get('fact_id')} missing source_id")

    return issues
