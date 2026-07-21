import json
from datetime import datetime, timezone

from digest.hermes_health import PROFILES, collect_health, render_usage_report, render_watchdog


def test_watchdog_reports_changed_issues_and_then_stays_silent(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    for name in PROFILES:
        root = home / "profiles" / name
        (root / "logs").mkdir(parents=True)
        (root / "cron").mkdir()
        (root / "gateway_state.json").write_text(json.dumps({"pid": 123}), encoding="utf-8")
        (root / "logs" / "gateway.log").write_text("Connected in websocket mode", encoding="utf-8")
        (root / "cron" / "jobs.json").write_text('{"jobs": []}', encoding="utf-8")
    monkeypatch.setattr("digest.hermes_health._pid_alive", lambda _pid: True)
    daily = tmp_path / "daily"
    bundle = daily / "digests" / "hermes" / "ai-intelligence-latest.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(json.dumps({"sources": {"daily_news_digest": {"status": "ok"}, "youtube_digest": {"status": "empty"}, "llm_wiki": {"status": "ok"}}}), encoding="utf-8")
    youtube = tmp_path / "youtube"
    run = youtube / "runs" / "r1"
    run.mkdir(parents=True)
    (youtube / "runs" / "latest_run.txt").write_text(str(run), encoding="utf-8")
    (run / "summary.md").write_text("# Pipeline Run Summary: ok", encoding="utf-8")
    report = collect_health(home, daily, youtube, datetime.now(timezone.utc))
    assert report["status"] == "WARNING"
    assert any(item["source"] == "youtube_digest" for item in report["issues"])
    state = tmp_path / "state.json"
    assert "youtube_digest" in render_watchdog(report, state)
    assert render_watchdog(report, state) == ""


def test_health_accepts_present_bundle_sources(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    for name in PROFILES:
        root = home / "profiles" / name
        (root / "logs").mkdir(parents=True)
        (root / "cron").mkdir()
        (root / "gateway_state.json").write_text(json.dumps({"pid": 123}), encoding="utf-8")
        (root / "logs" / "gateway.log").write_text("Connected in websocket mode", encoding="utf-8")
        (root / "cron" / "jobs.json").write_text('{"jobs": []}', encoding="utf-8")
    monkeypatch.setattr("digest.hermes_health._pid_alive", lambda _pid: True)
    daily = tmp_path / "daily"
    bundle = daily / "digests" / "hermes" / "ai-intelligence-latest.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(json.dumps({"sources": {
        "daily_news_digest": {"status": "present"},
        "youtube_digest": {"status": "present"},
        "llm_wiki": {"status": "present"},
    }}), encoding="utf-8")
    youtube = tmp_path / "youtube"
    run = youtube / "runs" / "r1"
    run.mkdir(parents=True)
    (youtube / "runs" / "latest_run.txt").write_text(str(run), encoding="utf-8")
    (run / "summary.md").write_text("# Pipeline Run Summary: ok", encoding="utf-8")

    assert collect_health(home, daily, youtube, datetime.now(timezone.utc))["status"] == "OK"


def test_usage_report_counts_only_recent_card_actions(tmp_path):
    root = tmp_path / "profiles" / "aisentinel" / "metrics" / "card-actions"
    root.mkdir(parents=True)
    (root / "a.json").write_text(json.dumps({"bot_action": "save", "created_at": "2026-07-14T09:00:00+08:00"}), encoding="utf-8")
    report = render_usage_report(tmp_path, datetime(2026, 7, 14, 12, tzinfo=timezone.utc))
    assert "aisentinel" in report
    assert "save 1 次" in report
    assert "聊天正文" in report
