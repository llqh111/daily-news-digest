import pytest

from digest import ai
from digest.quality import validate_digest_layout


def articles():
    return [
        {"article_id": f"a{i}", "category": category, "title": f"Source headline {i}",
         "source": "Publisher", "link": f"https://example.com/{i}",
         "evidence_card": {"coverage": {"has_fulltext": True},
                           "confirmed_facts": [{"text": "Source excerpt."}]}}
        for i, category in enumerate(("国际", "科技", "财经"))
    ]


def test_content_risk_has_a_typed_error_and_is_not_retried(monkeypatch):
    calls = []

    class Response:
        status_code = 400

        def raise_for_status(self):
            raise ai.requests.exceptions.HTTPError("400 Bad Request", response=self)

        def json(self):
            return {"error": {"message": "Content Exists Risk (request_id: example)"}}

    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(ai.requests, "post", post)
    with pytest.raises(ai.DeepSeekContentRejected):
        ai._call_deepseek_once("system", "user")
    assert len(calls) == 1


@pytest.mark.parametrize("batch_size,reject_at", [(7, 1), (1, 1), (1, 2), (1, 4)])
def test_rejection_preserves_all_sources_without_resubmitting(monkeypatch, batch_size, reject_at):
    selected = articles()
    calls = []
    monkeypatch.setattr(ai, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(ai, "BATCH_SIZE", batch_size)
    monkeypatch.setattr(ai, "load_recent_digests", lambda: "")
    monkeypatch.setattr(ai, "build_factcheck_notes", lambda _: "")

    def call(*args, **kwargs):
        calls.append(args)
        if len(calls) == reject_at:
            raise ai.DeepSeekContentRejected("Content Exists Risk")
        return {"choices": [{"message": {"content": "## 🌍 国际要闻\nDraft"}}]}

    monkeypatch.setattr(ai, "_call_deepseek_once", call)
    result = ai.summarize_with_deepseek(selected)
    validate_digest_layout(result, [a["article_id"] for a in selected])
    assert len(calls) == reject_at
    assert "原始来源快讯" in result
    assert "未生成 AI 分析" in result
    for article in selected:
        assert article["link"] in result
        assert article["title"] in result
        assert article["summary_mode"] == "source_only"


@pytest.mark.parametrize("status,message", [(400, "Invalid parameter"), (401, "Unauthorized"), (402, "Insufficient Balance"), (500, "Content Exists Risk")])
def test_other_api_errors_still_fail(monkeypatch, status, message):
    class Response:
        status_code = status

        def raise_for_status(self):
            raise ai.requests.exceptions.HTTPError(str(status), response=self)

        def json(self):
            return {"error": {"message": message}}

    monkeypatch.setattr(ai.requests, "post", lambda *args, **kwargs: Response())
    with pytest.raises(ai.requests.exceptions.HTTPError) as caught:
        ai._call_deepseek_once("system", "user")
    assert not isinstance(caught.value, ai.DeepSeekContentRejected)


@pytest.mark.parametrize("delivered", [True, False])
def test_pipeline_keeps_delivery_checks_and_skips_ai_after_rejection(monkeypatch, tmp_path, delivered):
    import main
    from digest import storage

    selected = [
        dict(article, title=f"Headline {i}", link=f"https://example.com/{i}")
        for i, article in enumerate(articles() * 3)
    ]
    sent = []
    ai_calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main.sys, "argv", ["main.py"])
    monkeypatch.setattr(storage, "SENT_LOG_FILE", str(tmp_path / "sent_articles.json"))
    monkeypatch.setattr(main, "should_skip_session", lambda: False)
    monkeypatch.setattr(main, "load_sent_links", lambda: set())
    monkeypatch.setattr(main, "fetch_all_feeds", lambda **kwargs: selected)
    monkeypatch.setattr(main, "triage_with_deepseek", lambda items: items)
    for name in ("scout_for_gaps", "pick_github_trending", "pick_signals"):
        monkeypatch.setattr(main, name, lambda: [])
    monkeypatch.setattr(main, "pick_bio_breakthrough", lambda: None)
    monkeypatch.setattr(main, "dedup_secondary", lambda titles, gaps, bio: (gaps, bio))
    for name in ("attach_fulltexts", "backfill_reference_depth", "tag_progress"):
        monkeypatch.setattr(main, name, lambda _: None)
    monkeypatch.setattr(main, "update_event_archive", lambda *args, **kwargs: {})
    for name in ("save_event_archive", "save_active_events_overview"):
        monkeypatch.setattr(main, name, lambda _: None)
    for name in ("export_run", "build_bundle"):
        monkeypatch.setattr(main, name, lambda *args: "test-artifact")
    monkeypatch.setattr(ai, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(ai, "load_recent_digests", lambda: "")

    def reject(*args, **kwargs):
        ai_calls.append(args)
        raise ai.DeepSeekContentRejected("Content Exists Risk")

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected content must not be submitted for AI rewrite/topics")

    monkeypatch.setattr(ai, "_call_deepseek_once", reject)
    monkeypatch.setattr(main, "refine_digest", forbidden)
    monkeypatch.setattr(main, "generate_topics", forbidden)
    monkeypatch.setattr(main, "SERVERCHAN_SENDKEY", "test-key")
    monkeypatch.setattr(main, "push_to_wechat", lambda summary, keys: sent.append(summary) or int(delivered))
    monkeypatch.setattr(main, "push_to_telegram", lambda _: False)
    monkeypatch.setattr(main, "send_failure_alert", lambda *args: None)

    if delivered:
        main.main()
        assert (tmp_path / "sent_articles.json").exists()
        assert len(list((tmp_path / "digests").glob("*.md"))) == 1
        assert len(list((tmp_path / "digests" / "quality").glob("*.json"))) == 1
    else:
        with pytest.raises(RuntimeError, match="所有推送渠道均失败"):
            main.main()
        assert not (tmp_path / "sent_articles.json").exists()
    assert len(ai_calls) == 1
    assert len(sent) == 1
    assert "article_id:" not in sent[0]
    assert all(article["link"] in sent[0] for article in selected)
