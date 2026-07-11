from digest.quality import (
    extract_and_normalize_numbers,
    parse_main_items,
    strip_internal_article_ids,
    validate_main_digest_evidence,
)


def test_parse_main_items():
    md = """
## 🌍 国际要闻

<!-- article_id:a1_123 -->
**🔥 苹果发布新手机**
苹果公司今天发布了iPhone 18。

<!-- article_id:a1_456 -->
**微软收购OpenAI**
作价2 billion美元。
"""
    items = parse_main_items(md)
    assert len(items) == 2
    assert items[0]["article_id"] == "a1_123"
    assert "iPhone 18" in items[0]["content"]


def test_extract_and_normalize_numbers():
    text = "Nvidia earned 2 billion dollars, a 30% increase."
    nums = extract_and_normalize_numbers(text)
    # expect 2e9 and 30
    assert 2000000000.0 in nums
    assert 30.0 in nums


def test_validate_main_digest_evidence():
    md = """
<!-- article_id:a1_123 -->
苹果利润达到 20 billion。
"""
    cards = [
        {
            "article_id": "a1_123",
            "numbers": ["20 billion", "10%"]
        }
    ]
    report = validate_main_digest_evidence(md, cards)
    assert report["total_items"] == 1
    assert report["items_with_unsupported_numbers"] == 0
    
    # 幻觉情况
    md2 = """
<!-- article_id:a1_123 -->
苹果利润达到 40 billion。
"""
    report2 = validate_main_digest_evidence(md2, cards)
    assert report2["items_with_unsupported_numbers"] == 1
    assert 40000000000.0 in report2["items"][0]["unsupported_list"]


def test_last_main_item_excludes_secondary_sections():
    markdown = """
<!-- article_id:a1_main -->
**Main news**
The main number is 20 billion.

## Secondary section
This is not main news and contains 99 billion.
"""
    cards = [{"article_id": "a1_main", "numbers": ["20 billion"]}]

    items = parse_main_items(markdown)
    report = validate_main_digest_evidence(markdown, cards)

    assert "99 billion" not in items[0]["content"]
    assert report["items_with_unsupported_numbers"] == 0


def test_strip_internal_article_ids_preserves_news_content():
    markdown = """<!-- article_id:a1_123 -->
**Title**
Body
"""

    assert strip_internal_article_ids(markdown) == "**Title**\nBody\n"

