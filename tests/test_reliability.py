"""Offline failure injection: no API calls or recipient messages."""
import pytest

from digest import ai, critique, triage
from digest.quality import validate_digest_layout


def candidates():
    return [
        dict(article_id=f"a{i}", category=category, title=f"Headline {i}",
             source="Publisher", link=f"https://example.com/{i}", score=100-i)
        for i, category in enumerate(["科技"] * 20 + ["国际"] * 6 + ["财经"] * 5)
    ]


def test_outage_selection_preserves_category_coverage(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("upstream unavailable")
    monkeypatch.setattr(triage, "_call_deepseek_once", fail)
    selected = triage.triage_with_deepseek(candidates())
    triage.ensure_main_news_coverage(selected)
    assert len(selected) <= 13
    assert all(a["triage_mode"] == "fallback" for a in selected)


@pytest.mark.parametrize("payload", [{"choices": []}, {"choices": None},
                                      {"choices": [{"message": {"content": None}}]}, []])
def test_malformed_api_json_degrades_to_sources(monkeypatch, payload):
    class Response:
        headers = {"Content-Type": "application/json"}
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    monkeypatch.setattr(ai, "DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: Response())
    monkeypatch.setattr(ai, "load_recent_digests", lambda: "")
    monkeypatch.setattr(ai, "build_factcheck_notes", lambda _: "")
    selected = [candidates()[i] for i in (0, 20, 26)]
    result = ai.summarize_with_deepseek(selected)
    validate_digest_layout(result, [a["article_id"] for a in selected])
    assert all(a["summary_mode"] == "source_only" for a in selected)


@pytest.mark.parametrize("damage", ["paragraph", "link", "section"])
def test_bad_rewrite_keeps_valid_original(damage):
    original = ai._render_source_digest([candidates()[i] for i in (0, 20, 26)])
    replacements = {
        "paragraph": ("【深层逻辑】", "背景分析"),
        "link": ("https://example.com/20", "https://unrelated.example/20"),
        "section": ("## 🌍 国际要闻", "## 国际"),
    }
    rewritten = original.replace(*replacements[damage])
    result = critique.refine_digest(original, enabled=True,
        evaluate=lambda _: {"overall": 1, "issues": []},
        revise=lambda *_: rewritten)
    assert result == original


def test_history_write_failure_preserves_previous_ledger(monkeypatch, tmp_path):
    import json
    from digest import storage
    path = tmp_path / "sent_articles.json"
    previous = {"history": {"2026-10-24": ["https://example.com/old"]}}
    path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(storage, "SENT_LOG_FILE", str(path))
    def fail(*_):
        raise OSError("disk unavailable")
    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(RuntimeError, match="推送记录"):
        storage.save_sent_links(["https://example.com/new"])
    assert json.loads(path.read_text(encoding="utf-8")) == previous


def test_thirty_days_with_outages_failed_delivery_and_duplicate_triggers(monkeypatch, tmp_path):
    """Exercise main + real storage across dates; all external I/O is stubbed."""
    import datetime as dt
    import json
    import main
    from digest import storage
    from digest.config import TZ

    real_datetime = dt.datetime
    clock = [real_datetime(2026, 10, 25, 8, tzinfo=TZ)]
    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(storage, "datetime", Clock)
    monkeypatch.setattr(ai, "datetime", Clock)
    monkeypatch.setattr(dt, "datetime", Clock)
    monkeypatch.setattr(storage, "SENT_LOG_FILE", str(tmp_path / "sent_articles.json"))
    monkeypatch.setattr(main.sys, "argv", ["main.py"])
    day = [0]
    def fetch(**kwargs):
        return [dict(a, article_id=f"d{day[0]}-{a['article_id']}",
                     link=f"{a['link']}/day{day[0]}") for a in candidates()]
    monkeypatch.setattr(main, "fetch_all_feeds", fetch)
    def unavailable(*args, **kwargs):
        raise ai.requests.exceptions.Timeout("injected outage")
    monkeypatch.setattr(triage, "_call_deepseek_once", unavailable)
    def generate(selected):
        if day[0] % 3 == 0:
            raise ai.DeepSeekContentRejected("injected rejection")
        if day[0] % 3 == 1:
            raise ai.requests.exceptions.Timeout("injected timeout")
        return ai._render_source_digest(selected)
    monkeypatch.setattr(ai, "_summarize_with_deepseek", generate)
    for name in ("scout_for_gaps", "pick_github_trending", "pick_signals"):
        monkeypatch.setattr(main, name, lambda: [])
    monkeypatch.setattr(main, "pick_bio_breakthrough", lambda: None)
    for name in ("attach_fulltexts", "backfill_reference_depth", "tag_progress"):
        monkeypatch.setattr(main, name, lambda _: None)
    monkeypatch.setattr(main, "dedup_secondary", lambda titles, gaps, bio: (gaps, bio))
    monkeypatch.setattr(main, "refine_digest", lambda summary, **kw: summary)
    monkeypatch.setattr(main, "generate_topics", lambda *args: "")
    monkeypatch.setattr(main, "update_event_archive", lambda *a, **k: {})
    for name in ("save_event_archive", "save_active_events_overview"):
        monkeypatch.setattr(main, name, lambda _: None)
    for name in ("export_run", "build_bundle"):
        monkeypatch.setattr(main, name, lambda *a: "test")
    monkeypatch.setattr(main, "send_failure_alert", lambda *a: None)
    monkeypatch.setattr(main, "SERVERCHAN_SENDKEY", "fake")
    monkeypatch.setattr(main, "push_to_telegram", unavailable)
    delivered, attempts = [], []
    fail_delivery = [False]
    def push(summary, keys):
        attempts.append(day[0])
        assert "article_id:" not in summary
        if fail_delivery[0]:
            return 0
        delivered.append(day[0])
        return 1
    monkeypatch.setattr(main, "push_to_wechat", push)

    for index in range(30):
        day[0] = index
        clock[0] = real_datetime(2026, 10, 25, 8, tzinfo=TZ) + dt.timedelta(days=index)
        if index % 5 == 0:
            fail_delivery[0] = True
            with pytest.raises(RuntimeError, match="所有推送渠道均失败"):
                main.main()
            assert not storage.should_skip_session()
        fail_delivery[0] = False
        main.main()
        assert storage.should_skip_session()
        main.main()  # duplicate scheduler trigger must not send again
        path = tmp_path / "digests" / f"{clock[0]:%Y-%m-%d}-AM.md"
        assert path.exists()
        ledger = json.loads((tmp_path / "sent_articles.json").read_text(encoding="utf-8"))
        assert ledger["last_push_timestamp"] == clock[0].timestamp()
    assert delivered == list(range(30))
    assert len(attempts) == 36
