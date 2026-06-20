"""#3 参考源深度天花板回填的单元测试（pytest, AAA 结构）。

monkeypatch 一律打在 *被使用处*：
- digest.backfill.web_search / digest.backfill.fetch_one_fulltext（按名导入到本模块）
- digest.backfill.BACKFILL_ENABLED / BACKFILL_MIN_SCORE / BACKFILL_MAX / FULLTEXT_MAX_CHARS
  （config 键也是按名导入，改 config.py 不影响已绑定的名字）
"""

from __future__ import annotations

import digest.backfill as backfill


def _enable(monkeypatch, *, min_score=8.0, max_count=3, max_chars=2800):
    """统一打开开关并固定预算阈值，避免依赖 config.py 真实值。"""
    monkeypatch.setattr(backfill, "BACKFILL_ENABLED", True)
    monkeypatch.setattr(backfill, "BACKFILL_MIN_SCORE", min_score)
    monkeypatch.setattr(backfill, "BACKFILL_MAX", max_count)
    monkeypatch.setattr(backfill, "FULLTEXT_MAX_CHARS", max_chars)
    monkeypatch.setattr(
        backfill, "PREFERRED_FULLTEXT_DOMAINS", ("bbc.co", "theguardian.com")
    )


def test_disabled_is_noop(monkeypatch):
    # Arrange
    monkeypatch.setattr(backfill, "BACKFILL_ENABLED", False)
    called = {"search": 0}
    monkeypatch.setattr(
        backfill, "web_search", lambda *a, **k: called.__setitem__("search", 1) or []
    )
    articles = [{"title": "X", "reference": True, "fulltext": "", "score": 9.0}]

    # Act
    backfill.backfill_reference_depth(articles)

    # Assert
    assert called["search"] == 0
    assert "fulltext" not in articles[0] or articles[0]["fulltext"] == ""
    assert "backfill_source" not in articles[0]


def test_ineligible_items_untouched(monkeypatch):
    # Arrange — non-reference / has-fulltext / low-score 都不该被碰
    _enable(monkeypatch)
    searched = []
    monkeypatch.setattr(
        backfill, "web_search", lambda q, num=5: searched.append(q) or []
    )
    monkeypatch.setattr(backfill, "fetch_one_fulltext", lambda art: "should not run")
    articles = [
        {"title": "non-ref", "reference": False, "fulltext": "", "score": 9.0},
        {"title": "has-ft", "reference": True, "fulltext": "已有正文", "score": 9.0},
        {"title": "low-score", "reference": True, "fulltext": "", "score": 5.0},
    ]

    # Act
    backfill.backfill_reference_depth(articles)

    # Assert
    assert searched == []
    for a in articles:
        assert "backfill_source" not in a


def test_backfills_whitelisted_domain_and_truncates(monkeypatch):
    # Arrange
    _enable(monkeypatch, max_chars=10)
    monkeypatch.setattr(
        backfill,
        "web_search",
        lambda q, num=5: [
            {"url": "https://spam.example.com/x"},
            {"url": "https://www.bbc.co/news/story"},
        ],
    )
    monkeypatch.setattr(backfill, "fetch_one_fulltext", lambda art: "A" * 50)
    art = {"title": "重要新闻", "reference": True, "fulltext": "", "score": 9.0}

    # Act
    backfill.backfill_reference_depth([art])

    # Assert
    assert art["fulltext"] == "A" * 10  # 截断到 FULLTEXT_MAX_CHARS
    assert art["backfill_source"] == "https://www.bbc.co/news/story"


def test_no_whitelisted_domain_leaves_item_untouched(monkeypatch):
    # Arrange
    _enable(monkeypatch)
    monkeypatch.setattr(
        backfill,
        "web_search",
        lambda q, num=5: [
            {"url": "https://spam.example.com/x"},
            {"url": "https://other.example.org/y"},
        ],
    )
    fetched = {"called": False}
    monkeypatch.setattr(
        backfill,
        "fetch_one_fulltext",
        lambda art: fetched.__setitem__("called", True) or "TEXT",
    )
    art = {"title": "重要新闻", "reference": True, "fulltext": "", "score": 9.0}

    # Act
    backfill.backfill_reference_depth([art])

    # Assert
    assert fetched["called"] is False
    assert "backfill_source" not in art
    assert art["fulltext"] == ""


def test_fetch_raises_degrades_without_crash(monkeypatch):
    # Arrange — 抓取抛异常 → 该条跳过、不崩溃（降级铁律）
    _enable(monkeypatch)
    monkeypatch.setattr(
        backfill,
        "web_search",
        lambda q, num=5: [{"url": "https://www.bbc.co/news/story"}],
    )

    def _boom(art):
        raise RuntimeError("network down")

    monkeypatch.setattr(backfill, "fetch_one_fulltext", _boom)
    art = {"title": "重要新闻", "reference": True, "fulltext": "", "score": 9.0}

    # Act — 不应抛出
    backfill.backfill_reference_depth([art])

    # Assert
    assert "backfill_source" not in art
    assert art["fulltext"] == ""


def test_backfill_max_cap_respected(monkeypatch):
    # Arrange — 给出 5 个合格目标，上限 2，断言 search 最多被调 2 次
    _enable(monkeypatch, max_count=2)
    counter = {"n": 0}

    def _search(q, num=5):
        counter["n"] += 1
        return []

    monkeypatch.setattr(backfill, "web_search", _search)
    monkeypatch.setattr(backfill, "fetch_one_fulltext", lambda art: "")
    articles = [
        {"title": f"news-{i}", "reference": True, "fulltext": "", "score": 9.0}
        for i in range(5)
    ]

    # Act
    backfill.backfill_reference_depth(articles)

    # Assert
    assert counter["n"] == 2
