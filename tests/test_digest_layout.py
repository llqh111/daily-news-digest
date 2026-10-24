import json

import pytest

from digest import ai
from digest.ai import _compose_batch_digest, strip_audit_block
from digest.quality import DigestLayoutError, validate_digest_layout
from main import _insert_gap_section, _insert_bio_section, _insert_github_section, _insert_signals_section, _prepend_one_minute


def body():
    parts = []
    for index, section in enumerate(("🌍 国际要闻", "💻 科技与 AI", "💰 财经市场")):
        parts.append(
            f"## {section}\n<!-- article_id:a{index} -->\n**标题{index}**\n"
            "- **【核心事实】**：事实。\n- **【深层逻辑】**：分析。\n"
            "- **【后市/影响】**：影响。\n> 📰 来源：媒体（https://example.com）"
        )
    return "\n\n".join(parts)


def test_json_narrative_cannot_move_or_duplicate_main_news():
    news = body()
    result = _compose_batch_digest(news, json.dumps({"lead": "今日导语正文。", "editorial": "编辑手记正文。"}))
    assert result.count(news) == 1
    assert result.index("今日导语正文") < result.index(news) < result.index("编辑手记正文")
    validate_digest_layout(result, ["a0", "a1", "a2"])


def test_october_5_wrapper_and_audit_placeholder_cannot_swallow_news():
    # 当天合并响应把全文套进 markdown 围栏，审计中又复述了占位符。
    narrative = "```markdown\n『今日导语』\n导语\n{{NEWS}}\n『编辑手记』\n手记\n```自我审计\n是否保留 {{NEWS}}？是。\n```\n```"
    result = strip_audit_block(_compose_batch_digest(body(), narrative))
    assert result == body()
    validate_digest_layout(result, ["a0", "a1", "a2"])


@pytest.mark.parametrize("narrative", [
    '{"lead":"导语。","editorial":"手记。"}',
    '```markdown\n{{NEWS}}\n```自我审计\n是否保留 {{NEWS}}？是。\n```\n```',
])
def test_real_batch_pipeline_preserves_every_article(monkeypatch, narrative):
    replies = iter(body().split("\n\n") + [narrative])
    monkeypatch.setattr(ai, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(ai, "BATCH_SIZE", 1)
    monkeypatch.setattr(ai, "_articles_to_text", lambda _: "候选素材")
    monkeypatch.setattr(ai, "load_recent_digests", lambda: "")
    monkeypatch.setattr(ai, "build_factcheck_notes", lambda _: "")
    monkeypatch.setattr(ai, "_call_deepseek_once", lambda *args, **kwargs: {
        "choices": [{"message": {"content": next(replies)}}]
    })
    articles = [
        {"article_id": f"a{index}", "category": category, "link": "https://example.com"}
        for index, category in enumerate(("国际", "科技", "财经"))
    ]
    result = strip_audit_block(ai.summarize_with_deepseek(articles))
    validate_digest_layout(result, ["a0", "a1", "a2"])
    assert all(result.count(f"article_id:a{index}") == 1 for index in range(3))


@pytest.mark.parametrize("lead", ["{{NEWS}}", "自我审计\n通过", "## 🌍 国际要闻\n正文", ["导语"]])
def test_invalid_narrative_falls_back_to_complete_news(lead):
    assert _compose_batch_digest(body(), json.dumps({"lead": lead, "editorial": "手记。"})) == body()


def test_plain_audit_is_removed_without_removing_following_section():
    text = body() + "\n\n自我审计\n1. 是否保留占位符？是。\n\n## 🎬 今日自媒体选题\n内容。"
    result = strip_audit_block(text)
    assert "自我审计" not in result and "是否保留" not in result
    assert body() in result and "今日自媒体选题" in result


def test_document_markdown_fence_is_unwrapped_before_audit_cleanup():
    text = "```markdown\n" + body() + "\n\n```自我审计\n内部问答\n```\n```"
    assert strip_audit_block(text) == body()


def test_audit_fence_is_removed_before_extra_sections_are_inserted():
    text = body() + "\n\n『编辑手记 / 今日看点』\n手记。\n\n```自我审计\n内部问答\n```"
    text = strip_audit_block(text)
    text = _insert_gap_section(text, [{"title": "信息差"}])
    text = _insert_bio_section(text, {"title": "生物"})
    text = _insert_github_section(text, [{"full_name": "owner/repo"}])
    text = _insert_signals_section(text, [{"title": "工具"}])
    validate_digest_layout(text, ["a0", "a1", "a2"])
    assert text.index("💰 财经市场") < text.index("💡 信息差") < text.index("编辑手记")


def test_quick_read_and_digest_have_a_blank_line_boundary():
    articles = [{"title": "标题", "article_id": "a0", "category": "国际"}]
    result = _prepend_one_minute(body(), articles)
    assert "\n\n## 🌍 国际要闻" in result
    validate_digest_layout(result, ["a0", "a1", "a2"])


@pytest.mark.parametrize("bad", [
    lambda text: "## 💡 信息差侦察\n信息差\n\n" + text,
    lambda text: text.replace("## 🌍 国际要闻", "2. 是否使用 ## 🌍 国际要闻"),
    lambda text: text.replace("article_id:a2", "article_id:a1"),
    lambda text: text.replace("【后市/影响】", "后市"),
    lambda text: text.replace("> 📰 来源：媒体（https://example.com）", ""),
    lambda text: text + "\n自我审计\n问答",
    lambda text: text + "\n```",
    lambda text: "```markdown\n" + text + "\n```",
])
def test_layout_gate_blocks_corrupt_or_incomplete_delivery(bad):
    with pytest.raises(DigestLayoutError):
        validate_digest_layout(bad(body()), ["a0", "a1", "a2"])


@pytest.mark.parametrize("damage", [
    lambda text: text[:text.index("## 💰 财经市场")],
    lambda text: text.replace("article_id:a2", "article_id:wrong"),
    lambda text: text + "\n" + body().split("\n\n")[2],
    lambda text: text.replace("https://example.com", "https://wrong.example", 1),
])
def test_invalid_batch_repairs_only_broken_articles(monkeypatch, damage):
    articles = [
        {"article_id": f"a{index}", "category": category, "link": "https://example.com",
         "source": "媒体", "title": f"标题{index}"}
        for index, category in enumerate(("国际", "科技", "财经"))
    ]
    calls = []
    monkeypatch.setattr(ai, "_articles_to_text", lambda rows: rows[0]["article_id"])

    def respond(system, prompt, **kwargs):
        aid = prompt[-2:]
        calls.append(aid)
        assert kwargs["thinking_enabled"] is False
        return {"choices": [{"message": {"content": body().split("\n\n")[int(aid[1])]}}]}

    monkeypatch.setattr(ai, "_call_deepseek_once", respond)
    result = ai._repair_batch_output(damage(body()), articles)
    validate_digest_layout(result, ["a0", "a1", "a2"])
    # A missing/substituted citation is supplied from RSS metadata, not re-generated by AI.
    assert len(calls) == (0 if "wrong.example" in damage(body()) else 1)
    assert "wrong" not in result


def test_model_missing_source_is_restored_from_selected_article(monkeypatch):
    article = {"article_id": "a0", "category": "国际", "link": "https://source.example/story",
               "source": "Publisher", "title": "Original headline"}
    draft = body().split("\n\n")[0].replace("> 📰 来源：媒体（https://example.com）", "")
    monkeypatch.setattr(ai, "_call_deepseek_once", lambda *a, **k: pytest.fail("来源不应交给模型补写"))
    result = ai._repair_batch_output(draft, [article])
    assert result.count("https://source.example/story") == 1
    validate_digest_layout(result, ["a0"])


def test_failed_model_repair_falls_back_to_labeled_source_item(monkeypatch):
    article = {"article_id": "a0", "category": "国际", "link": "https://source.example/story",
               "source": "Publisher", "title": "Original headline"}
    monkeypatch.setattr(ai, "_articles_to_text", lambda _: "素材")
    monkeypatch.setattr(ai, "_call_deepseek_once", lambda *a, **k: {
        "choices": [{"message": {"content": "没有新闻正文"}}]
    })
    result = ai._repair_batch_output("", [article])
    validate_digest_layout(result, ["a0"])
    assert "原始来源快讯" in result
    assert "Original headline" in result
    assert article["summary_mode"] == "source_only"


def test_single_batch_cannot_keep_a_substituted_source_link(monkeypatch):
    selected = [
        {"article_id": f"a{i}", "category": category, "title": f"Headline {i}",
         "source": "Publisher", "link": f"https://original.example/{i}"}
        for i, category in enumerate(("国际", "科技", "财经"))
    ]
    calls = []
    monkeypatch.setattr(ai, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(ai, "BATCH_SIZE", 7)
    monkeypatch.setattr(ai, "load_recent_digests", lambda: "")
    monkeypatch.setattr(ai, "build_factcheck_notes", lambda _: "")

    def respond(*args, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"content": body()}}]}

    monkeypatch.setattr(ai, "_call_deepseek_once", respond)
    result = ai.summarize_with_deepseek(selected)
    assert len(calls) == 1
    validate_digest_layout(result, [item["article_id"] for item in selected])
    assert all(item["link"] in result for item in selected)
    assert "https://example.com" not in result


def test_batch_source_fallback_skips_ai_editorial(monkeypatch):
    selected = [
        {"article_id": f"a{i}", "category": category, "title": f"Headline {i}",
         "source": "Publisher", "link": "https://example.com"}
        for i, category in enumerate(("国际", "科技", "财经"))
    ]
    replies = iter(["missing item", "still missing", *body().split("\n\n")[1:]])
    calls = []
    monkeypatch.setattr(ai, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(ai, "BATCH_SIZE", 1)
    monkeypatch.setattr(ai, "load_recent_digests", lambda: "")
    monkeypatch.setattr(ai, "build_factcheck_notes", lambda _: "")

    def respond(*args, **kwargs):
        calls.append(1)
        return {"choices": [{"message": {"content": next(replies)}}]}

    monkeypatch.setattr(ai, "_call_deepseek_once", respond)
    result = ai.summarize_with_deepseek(selected)
    validate_digest_layout(result, [item["article_id"] for item in selected])
    assert selected[0]["summary_mode"] == "source_only"
    assert len(calls) == 4
    assert "编辑手记" not in result


def test_failed_item_repair_still_blocks_delivery(monkeypatch):
    article = {"article_id": "a2", "category": "财经", "link": "https://example.com"}
    monkeypatch.setattr(ai, "_articles_to_text", lambda _: "素材")
    monkeypatch.setattr(ai, "_call_deepseek_once", lambda *a, **k: {
        "choices": [{"message": {"content": "没有新闻正文"}}]
    })
    with pytest.raises(DigestLayoutError, match="缺少原始来源元数据"):
        ai._repair_batch_output("", [article])


def test_single_batch_repair_does_not_embed_old_editorial_in_news(monkeypatch):
    articles = [
        {"article_id": f"a{index}", "category": category, "link": "https://example.com"}
        for index, category in enumerate(("国际", "科技", "财经"))
    ]
    monkeypatch.setattr(ai, "_call_deepseek_once", lambda *a, **k: pytest.fail("完整条目不应重写"))
    result = ai._repair_batch_output(body() + "\n『编辑手记 / 今日看点』\n旧手记", articles)
    validate_digest_layout(result, ["a0", "a1", "a2"])
    assert "旧手记" not in result
