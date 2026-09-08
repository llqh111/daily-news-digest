import json
from datetime import datetime

from digest.config import TZ
from digest.events import active_events, update_event_archive
from digest.evidence import evidence_notice
from digest.weekly import collect_weekly_items, render_weekly_review
from main import _insert_evidence_notices, _prepend_one_minute


def _article(title, article_id, *, progress_of=None):
    return {
        "title": title,
        "zh": title,
        "article_id": article_id,
        "ai_reason": "重要进展",
        "cluster_size": 2,
        "progress_of": progress_of,
        "evidence_card": {
            "confirmed_facts": [{"text": "已确认的关键事实"}],
            "unknowns": ["执行细节未公布"],
            "sources": [{"url": "https://example.com/a"}],
            "coverage": {"has_fulltext": True},
        },
    }


def test_event_archive_updates_only_confirmed_link(tmp_path):
    path = tmp_path / "events.json"
    first = _article("Company launches product", "a1")
    update_event_archive([first], datetime(2026, 9, 1, 8, tzinfo=TZ), str(path))

    updated = _article(
        "Company product opens to users", "a2",
        progress_of={"prev_zh": "Company launches product", "date": "2026-09-01"},
    )
    unrelated = _article("Company launches unrelated product", "a3")
    payload = update_event_archive([updated, unrelated], datetime(2026, 9, 2, 8, tzinfo=TZ), str(path))

    assert updated["event"]["id"] == first["event"]["id"]
    assert unrelated["event"]["id"] != first["event"]["id"]
    assert len(payload["events"][0]["updates"]) == 2


def test_active_events_excludes_stale_entries(tmp_path):
    path = tmp_path / "events.json"
    article = _article("Fresh event", "a1")
    payload = update_event_archive([article], datetime(2026, 7, 1, 8, tzinfo=TZ), str(path))
    assert active_events(payload, datetime(2026, 9, 1, 8, tzinfo=TZ)) == []


def test_weekly_uses_only_complete_artifacts(tmp_path):
    root = tmp_path
    (root / "digests" / "meta").mkdir(parents=True)
    (root / "digests" / "quality").mkdir(parents=True)
    (root / "digests" / "quality" / "2026-09-06-AM.json").write_text(json.dumps({"total_items": 1}), encoding="utf-8")
    (root / "digests" / "2026-09-06-AM.md").write_text("digest", encoding="utf-8")
    (root / "digests" / "meta" / "2026-09-06-AM-evidence.json").write_text(
        json.dumps({"items": [{"headline": "Important", "relevance_score": 9, "coverage": {"has_fulltext": True}}]}),
        encoding="utf-8",
    )
    (root / "sent_articles.json").write_text(json.dumps({"delivery_runs": {
        "2026-09-06-AM": {"artifact_bundle_status": "present", "delivered_at": "2026-09-06T08:00:00+08:00"},
        "2026-09-06-PM": {"artifact_bundle_status": "missing"},
    }}), encoding="utf-8")
    items = collect_weekly_items(root, datetime(2026, 9, 8, 9, tzinfo=TZ))
    assert [item["headline"] for item in items] == ["Important"]
    review = render_weekly_review(items, {"events": []}, datetime(2026, 9, 8, 9, tzinfo=TZ))
    assert "本周值得回看" in review


def test_evidence_notice_is_limited_to_material_gaps():
    article = _article("Full text", "a1")
    assert evidence_notice(article) == ""
    article["cluster_size"] = 1
    assert "单一可核对信源" in evidence_notice(article)
    article["evidence_card"]["coverage"] = {"has_fulltext": False}
    assert "标题或摘要" in evidence_notice(article)


def test_daily_layers_render_three_cards_and_matched_notice():
    articles = [_article(f"Story {index}", f"a{index}") for index in range(4)]
    for index, article in enumerate(articles):
        article["ai_score"] = 10 - index
    body = "\n".join(
        f"<!-- article_id:a{index} -->\n**中文标题 {index}**\n- **【核心事实】**：中文新增事实 {index}\n"
        for index in range(4)
    )
    layered = _prepend_one_minute(body, articles)
    assert layered.count("### ") == 3
    assert "中文标题 0" in layered
    assert "中文新增事实 0" in layered
    articles[0]["evidence_notice"] = "材料不足"
    rendered = _insert_evidence_notices("<!-- article_id:a0 -->\n**新闻**", articles)
    assert "> ⚠️ 证据提示：材料不足" in rendered


def test_daily_save_preserves_delivery_history(tmp_path, monkeypatch):
    from digest import storage
    target = tmp_path / "sent_articles.json"
    runs = {"2026-09-01-AM": {"artifact_bundle_status": "present"}}
    target.write_text(json.dumps({"history": {}, "delivery_runs": runs}), encoding="utf-8")
    monkeypatch.setattr(storage, "SENT_LOG_FILE", str(target))
    storage.save_sent_links(["https://example.com/new"])
    assert json.loads(target.read_text(encoding="utf-8"))["delivery_runs"] == runs


def test_weekly_second_run_does_not_send(tmp_path, monkeypatch):
    import main
    monkeypatch.chdir(tmp_path)
    sent = []
    monkeypatch.setattr(main, "SERVERCHAN_SENDKEY", "")
    monkeypatch.setattr(main, "build_weekly_review", lambda *args: "本周变化")
    monkeypatch.setattr(main, "push_to_telegram", lambda content: sent.append(content) or True)
    main.run_weekly()
    main.run_weekly()
    assert sent == ["本周变化"]
