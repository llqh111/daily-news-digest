import json

import pytest

from digest.hermes_knowledge_notification import (
    KnowledgeNotificationError,
    _resolve_feishu_target,
    deliver_notification,
    render_notification,
)


def _artifact(root, run_id="run-1", entries=None):
    entries = [] if entries is None else entries
    path = root / "run.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "promotion_batch": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def _entry(index=1):
    return {
        "entry_id": f"entry-{index}",
        "claim": f"Claim {index}",
        "when_to_use": "When useful",
        "when_not_to_use": "When not useful",
        "evidence": [{"source_id": "official", "title": "Evidence", "url": "https://example.com"}],
        "topic_page": "ai/workflows/vibe-coding.md",
    }


def test_delivers_once_and_renders_confirmation_boundary(tmp_path):
    artifact = _artifact(tmp_path, entries=[_entry()])
    ledger = tmp_path / "ledger.json"
    sent = []

    first = deliver_notification(
        artifact, ledger, sender=lambda message, profile, target: sent.append((message, profile, target))
    )
    second = deliver_notification(
        artifact, ledger, sender=lambda message, profile, target: sent.append((message, profile, target))
    )

    assert first == {"run_id": "run-1", "status": "delivered", "count": 1}
    assert second == {"run_id": "run-1", "status": "duplicate", "count": 1}
    assert len(sent) == 1
    assert sent[0][1:] == ("aisentinel", "feishu")
    assert "待研读的临时判断" in sent[0][0]
    assert "entry-1" in sent[0][0]


def test_zero_items_is_success_without_sending(tmp_path):
    artifact = _artifact(tmp_path)
    ledger = tmp_path / "ledger.json"
    sent = []

    result = deliver_notification(
        artifact, ledger, sender=lambda message, profile, target: sent.append(message)
    )

    assert result == {"run_id": "run-1", "status": "empty", "count": 0}
    assert sent == []
    assert json.loads(ledger.read_text(encoding="utf-8"))["deliveries"]["run-1"]["status"] == "empty"


def test_rejects_more_than_five_or_incomplete_proposals(tmp_path):
    ledger = tmp_path / "ledger.json"
    with pytest.raises(KnowledgeNotificationError, match="five-proposal"):
        deliver_notification(_artifact(tmp_path, entries=[_entry(i) for i in range(6)]), ledger)

    broken = _entry()
    broken["evidence"] = []
    with pytest.raises(KnowledgeNotificationError, match="evidence"):
        deliver_notification(_artifact(tmp_path, entries=[broken]), ledger)


def test_resolves_one_existing_profile_cron_target_without_extra_mapping(monkeypatch, tmp_path):
    profile = tmp_path / "hermes/profiles/aisentinel/cron"
    profile.mkdir(parents=True)
    (profile / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"enabled": True, "deliver": "feishu:oc_test"},
                    {"enabled": True, "deliver": "feishu:oc_test"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert _resolve_feishu_target("aisentinel") == "feishu:oc_test"
