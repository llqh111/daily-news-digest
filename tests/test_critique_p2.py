from digest.critique import _article_id_count, refine_digest
import digest.critique as critique

def test_article_id_count():
    text = """
<!-- article_id:a1_123 -->
**标题**
正文
<!-- article_id:a1_456 -->
    """
    assert _article_id_count(text) == 2

def test_refine_digest_article_id_fallback():
    # 模拟重写丢失 article_id
    summary = """
<!-- article_id:a1_123 -->
**新闻一**
> 📰 来源：A
"""
    # revise 故意丢失 ID
    def mock_revise(*args, **kwargs):
        return """
**新闻一重写**
> 📰 来源：A
"""
    
    # 因为总分足够低触发 revise
    def mock_evaluate(*args):
        return {"overall": 5, "issues": ["问题"]}
        
    rewritten = refine_digest(summary, enabled=True, evaluate=mock_evaluate, revise=mock_revise)
    
    # 应该回退到 summary
    assert rewritten == summary

def test_refine_digest_rejects_reordered_article_ids():
    summary = "<!-- article_id:a1_123 -->\nA\n<!-- article_id:a1_456 -->\nB\n> source"

    def mock_revise(*args, **kwargs):
        return "<!-- article_id:a1_456 -->\nA\n<!-- article_id:a1_123 -->\nB\n> source"

    rewritten = refine_digest(
        summary,
        enabled=True,
        evaluate=lambda *_: {"overall": 5, "issues": ["issue"]},
        revise=mock_revise,
    )

    assert rewritten == summary


def test_observe_mode_does_not_rewrite_for_evidence_warning(monkeypatch):
    monkeypatch.setattr(critique, "EVIDENCE_GUIDED_REWRITE_ENABLED", False)
    summary = "<!-- article_id:a1_123 -->\n**News**\n> source: A\n"
    called = {"revise": False}

    def mock_revise(*args, **kwargs):
        called["revise"] = True
        return "rewritten"

    result = refine_digest(
        summary,
        enabled=True,
        quality_report={
            "items_with_unsupported_numbers": 1,
            "items": [
                {
                    "article_id": "a1_123",
                    "has_unsupported_numbers": True,
                    "unsupported_list": [99],
                }
            ],
        },
        evaluate=lambda *_: {"overall": 10, "issues": []},
        revise=mock_revise,
    )

    assert result == summary
    assert called["revise"] is False
