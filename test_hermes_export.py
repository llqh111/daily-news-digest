import json

import pytest

from digest.hermes_export import HermesExportError, export_run


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _make_verified_run(tmp_path, run_key="2026-07-14-AM"):
    _write_json(tmp_path / "sent_articles.json", {
        "delivery_runs": {run_key: {"delivered_at": "2026-07-14T08:02:00+08:00", "artifact_bundle_status": "present"}}
    })
    (tmp_path / "digests").mkdir()
    (tmp_path / "digests" / f"{run_key}.md").write_text("# digest", encoding="utf-8")
    _write_json(tmp_path / "digests" / "meta" / f"{run_key}-evidence.json", {
        "date": "2026-07-14", "session": "AM", "items": [{
            "article_id": "a1_test", "headline": "AI release", "summary": "Short summary", "category": "technology", "published_at": "2026-07-14T07:00:00+08:00", "why_relevant": "Relevant", "relevance_score": 8, "confirmed_facts": ["A fact"],
            "entities": ["Example"], "numbers": ["2"], "dates": [], "unknowns": [],
            "coverage": {"has_fulltext": True},
            "sources": [{"role": "primary", "canonical_url": "https://example.com/a", "publisher": "Example"}],
        }]
    })
    _write_json(tmp_path / "digests" / "quality" / f"{run_key}.json", {
        "total_items": 1, "items_with_unsupported_numbers": 0, "unsupported_ratio": 0.0
    })


def test_export_run_writes_verified_run_and_latest(tmp_path):
    _make_verified_run(tmp_path)

    output = export_run(tmp_path, "2026-07-14-AM")

    exported = json.loads(output.read_text(encoding="utf-8"))
    latest = json.loads((tmp_path / "digests" / "hermes" / "latest.json").read_text(encoding="utf-8"))
    assert exported["run_key"] == "2026-07-14-AM"
    assert exported["items"][0]["url"] == "https://example.com/a"
    assert exported["items"][0]["summary"] == "Short summary"
    assert exported["items"][0]["evidence_complete"] is True
    assert exported["delivery_successful"] is True
    assert latest == exported


def test_export_run_rejects_incomplete_artifact_bundle(tmp_path):
    _make_verified_run(tmp_path)
    ledger_path = tmp_path / "sent_articles.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["delivery_runs"]["2026-07-14-AM"]["artifact_bundle_status"] = "missing"
    _write_json(ledger_path, ledger)

    with pytest.raises(HermesExportError, match="不完整"):
        export_run(tmp_path, "2026-07-14-AM")
