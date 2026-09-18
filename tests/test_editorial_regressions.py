import math

from digest.scoring import cluster_and_boost, merge_similar_clusters
from digest.quality import extract_and_normalize_numbers, validate_main_digest_evidence
from main import _prepend_one_minute


def article(title, score, category="科技"):
    return {"title": title, "score": score, "ai_score": score, "source": title,
            "category": category, "article_id": title, "link": "https://example.com"}


def test_shared_companies_are_not_a_shared_event():
    items = [article("Microsoft Anthropic announce cloud contract", 10),
             article("Microsoft Anthropic debate safety regulation", 9)]
    assert len(cluster_and_boost(items)) == 2


def test_lexical_cluster_cannot_grow_via_union_of_unrelated_words():
    items = [article("Ukraine counteroffensive begins in Kharkiv region", 10),
             article("Ukraine counteroffensive continues as forces advance", 9),
             article("Kharkiv forces open hospital for veterans", 8)]
    assert len(cluster_and_boost(items)) == 2


def test_semantic_cluster_requires_all_pairs_not_transitive_chain():
    items = [article("A", 10), article("B", 9), article("C", 8)]
    vectors = [[math.cos(a), math.sin(a)] for a in (0, 0.4, 0.8)]
    result = merge_similar_clusters(items, threshold=0.9, embed_fn=lambda _: vectors)
    assert len(result) == 2


def test_quick_read_covers_categories_and_uses_substantive_impact():
    items = [article("tech1", 10), article("tech2", 9), article("tech3", 8),
             article("world", 7, "国际"), article("finance", 6, "财经")]
    for item in items:
        item["ai_reason"] = "选稿标签"
        item["event"] = {"open_questions": ["缺少明确时间"]}
    body = "\n".join(
        f'<!-- article_id:{item["article_id"]} -->\n**{item["title"]}**\n'
        '- **【核心事实】**：已确认事实。\n- **【深层逻辑】**：分析。\n'
        '- **【后市/影响】**：这会改变采购成本。更多细节。\n'
        for item in items
    )
    lead = _prepend_one_minute(body, items).split("<!-- article_id:")[0]
    assert "world" in lead and "finance" in lead
    assert "tech2" not in lead and "选稿标签" not in lead
    assert "缺少明确时间" not in lead
    assert "这会改变采购成本" in lead


def test_chinese_numbers_match_english_evidence_and_find_new_claims():
    assert 24_300_000_000 in extract_and_normalize_numbers("243亿美元")
    assert 3300 in extract_and_normalize_numbers("3,300人")
    cards = [{"article_id": "a1", "numbers": ["$24.3 billion", "48 aircraft"]}]
    good = validate_main_digest_evidence("<!-- article_id:a1 -->\n金额243亿美元。", cards)
    assert good["items_with_unsupported_numbers"] == 0
    bad = validate_main_digest_evidence("<!-- article_id:a1 -->\n造成177人死亡。", cards)
    assert bad["items_with_unsupported_numbers"] == 1


def test_quality_exposes_missing_cards_and_unchecked_articles():
    report = validate_main_digest_evidence(
        "<!-- article_id:wrong -->\n正文。", [{"article_id": "expected", "numbers": []}])
    assert report["items_with_missing_evidence"] == 1
    assert report["unmatched_evidence_ids"] == ["expected"]
    assert report["validation_complete"] is False
