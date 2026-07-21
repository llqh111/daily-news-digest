import json

import pytest

from digest.hermes_curator_handoff import HandoffError, create_handoff


def _bundle(root, items):
    path = root / "digests" / "hermes" / "ai-intelligence-latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": "2026-07-14", "items": items}), encoding="utf-8")


def test_handoff_is_idempotent_and_confirmation_only(tmp_path):
    _bundle(tmp_path, [{"article_id": "daily:a1", "title": "AI", "url": "https://example.com/a", "evidence_path": "evidence.json"}])

    first = create_handoff(tmp_path, "daily:a1")
    second = create_handoff(tmp_path, "daily:a1")
    payload = json.loads(first.read_text(encoding="utf-8"))

    assert first == second
    assert payload["schema_version"] == 2
    assert payload["status"] == "pending"
    assert payload["source_bot"] == "aisentinel"
    assert payload["target_bot"] == "knowledgecurator"
    assert payload["idempotency_key"].startswith("handoff:")
    assert payload["confirmed_at"] is None
    assert payload["consumed_at"] is None
    assert "Do not write into llm-wiki" in payload["allowed_next_action"]


def test_handoff_rejects_unknown_or_untraceable_item(tmp_path):
    _bundle(tmp_path, [{"article_id": "daily:a1", "title": "AI", "url": "", "evidence_path": ""}])

    with pytest.raises(HandoffError, match="not found"):
        create_handoff(tmp_path, "missing")
    with pytest.raises(HandoffError, match="traceable"):
        create_handoff(tmp_path, "daily:a1")


def test_existing_v1_handoff_is_upgraded_without_duplication(tmp_path):
    _bundle(tmp_path, [{"article_id": "daily:a1", "title": "AI", "url": "https://example.com/a", "evidence_path": "evidence.json"}])
    output = create_handoff(tmp_path, "daily:a1")
    old = json.loads(output.read_text(encoding="utf-8"))
    old["schema_version"] = 1
    old["status"] = "awaiting_user_confirmation"
    output.write_text(json.dumps(old), encoding="utf-8")
    upgraded = json.loads(create_handoff(tmp_path, "daily:a1").read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == 2
    assert upgraded["status"] == "pending"
    assert upgraded["created_at"] == old["created_at"]
