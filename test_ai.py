"""Regression tests for deterministic batch-rendering cleanup."""

from digest.ai import _is_empty_section_placeholder


def test_empty_batch_section_placeholder_is_removed():
    assert _is_empty_section_placeholder("（本批暂无符合该板块的候选新闻。）")


def test_real_news_text_is_not_treated_as_placeholder():
    assert not _is_empty_section_placeholder("国际要闻：一项新协议今日生效")
