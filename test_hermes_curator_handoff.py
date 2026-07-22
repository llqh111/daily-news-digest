import json

import pytest

from digest.hermes_curator_handoff import (
    HandoffError,
    create_handoff,
    create_knowledge_promotion_handoff,
)


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
    assert payload["schema_version"] == 3
    assert payload["source_type"] == "intelligence_item"
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
    assert upgraded["schema_version"] == 3
    assert upgraded["status"] == "pending"
    assert upgraded["created_at"] == old["created_at"]


def test_knowledge_promotion_handoff_is_v3_idempotent_and_traceable(tmp_path):
    wiki = tmp_path / "wiki"
    run_path = wiki / "reports/AI/runs/run-1.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "production_writes_enabled": False,
                "promotion_batch": [
                    {
                        "entry_id": "entry-1",
                        "claim": "Use an observable acceptance seam.",
                        "when_to_use": "When delegating coding work.",
                        "when_not_to_use": "For open-ended exploration.",
                        "evidence": [
                            {"source_id": "official", "title": "Evidence", "url": "https://example.com"}
                        ],
                        "topic_page": "ai/workflows/vibe-coding.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    first = create_knowledge_promotion_handoff(tmp_path, wiki, "run-1", "entry-1")
    second = create_knowledge_promotion_handoff(tmp_path, wiki, "run-1", "entry-1")
    payload = json.loads(first.read_text(encoding="utf-8"))

    assert first == second
    assert payload["schema_version"] == 3
    assert payload["source_type"] == "knowledge_promotion"
    assert payload["run_id"] == "run-1"
    assert payload["entry_id"] == "entry-1"
    assert payload["allowed_next_actions"] == ["confirm", "revise", "withdraw"]
    assert payload["production_writes_enabled"] is False
    assert payload["status"] == "pending"


def test_knowledge_promotion_handoff_rejects_missing_entry(tmp_path):
    wiki = tmp_path / "wiki"
    run_path = wiki / "reports/AI/runs/run-1.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(
        json.dumps({"schema_version": 1, "run_id": "run-1", "promotion_batch": []}),
        encoding="utf-8",
    )
    with pytest.raises(HandoffError, match="not found"):
        create_knowledge_promotion_handoff(tmp_path, wiki, "run-1", "missing")
