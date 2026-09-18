import os
import json
from datetime import datetime, timedelta
from digest.storage import save_evidence_sidecar, save_quality_report, prune_quality_artifacts, mark_artifact_bundle_status
from digest.config import TZ


def test_collected_datetime_survives_evidence_save_and_weekly_read(tmp_path, monkeypatch):
    from digest.evidence import build_evidence_cards
    from digest.weekly import collect_weekly_items

    monkeypatch.chdir(tmp_path)
    now = datetime.now(TZ)
    session = "AM" if 4 <= now.hour < 16 else "PM"
    run_key = f"{now:%Y-%m-%d}-{session}"
    articles = [{"title": "Example release", "source": "Example",
                 "link": "https://example.com/release", "published": now}]
    build_evidence_cards(articles)
    assert save_evidence_sidecar(articles)
    assert save_quality_report({"total_items": 1})
    (tmp_path / "digests" / f"{run_key}.md").write_text("digest", encoding="utf-8")
    (tmp_path / "sent_articles.json").write_text(json.dumps({"delivery_runs": {
        run_key: {"delivered_at": now.isoformat(), "artifact_bundle_status": "present"}
    }}), encoding="utf-8")
    items = collect_weekly_items(tmp_path, now)
    assert len(items) == 1
    assert items[0]["published_at"] == now.isoformat()
    assert not list((tmp_path / "digests" / "meta").glob("*.tmp"))

def test_atomic_write_and_prune(tmp_path, monkeypatch):
    # Mock dirs
    meta_dir = tmp_path / "digests" / "meta"
    quality_dir = tmp_path / "digests" / "quality"

    # redirect os.makedirs, os.path.join, etc to use tmp_path
    orig_join = os.path.join
    def mock_join(*args):
        # intercept "digests", "meta"
        if args[0] == "digests":
            return str(tmp_path / args[0] / args[1])
        return orig_join(*args)

    monkeypatch.setattr(os.path, "join", mock_join)

    # create dummy files with old dates in name but new date in json
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(quality_dir, exist_ok=True)

    # old file by name
    old_file_path = meta_dir / "2020-01-01-AM-evidence.json"
    with open(old_file_path, "w") as f:
        json.dump({"date": "2026-07-11"}, f)

    now = datetime.now(TZ)
    prune_quality_artifacts(now)

    # Should be deleted because it relies on the filename (2020-01-01) which is old
    assert not os.path.exists(old_file_path)

def test_mark_artifact_bundle_status(tmp_path, monkeypatch):
    log_file = tmp_path / "sent_articles.json"
    monkeypatch.setattr("digest.storage.SENT_LOG_FILE", str(log_file))

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump({"history": {}}, f)

    res = mark_artifact_bundle_status("2026-07-11-AM", True, False)
    assert res is True

    with open(log_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["delivery_runs"]["2026-07-11-AM"]["evidence_status"] == "present"
    assert data["delivery_runs"]["2026-07-11-AM"]["quality_status"] == "missing"
    assert data["delivery_runs"]["2026-07-11-AM"]["artifact_bundle_status"] == "missing"
