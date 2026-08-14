import json

import pytest

from digest import hermes_knowledge_action as actions


def test_review_maps_to_authoritative_cli(monkeypatch, tmp_path):
    observed = {}

    def fake_run(root, arguments):
        observed["root"] = root
        observed["arguments"] = arguments
        decisions = json.loads(open(arguments[arguments.index("--decisions") + 1], encoding="utf-8").read())
        observed["decisions"] = decisions
        return [{"entry_id": "entry-1", "status": "confirmed"}]

    monkeypatch.setattr(actions, "_run_wiki_cli", fake_run)
    result = actions.apply_review(
        tmp_path, entry_id="entry-1", action="confirm", reviewed_on="2026-07-22"
    )

    assert result[0]["status"] == "confirmed"
    assert observed["arguments"][0] == "review"
    assert observed["decisions"] == [{"entry_id": "entry-1", "action": "confirm"}]


def test_review_requires_revision_or_withdrawal_detail(tmp_path):
    with pytest.raises(actions.KnowledgeActionError, match="claim"):
        actions.apply_review(tmp_path, entry_id="entry-1", action="revise", reviewed_on="2026-07-22")
    with pytest.raises(actions.KnowledgeActionError, match="reason"):
        actions.apply_review(tmp_path, entry_id="entry-1", action="withdraw", reviewed_on="2026-07-22")


@pytest.mark.parametrize(
    ("feedback_type", "expected"),
    [("retrieved", "feedback-retrieved"), ("cited", "feedback-cited")],
)
def test_feedback_maps_to_authoritative_cli(monkeypatch, tmp_path, feedback_type, expected):
    observed = []
    monkeypatch.setattr(actions, "_run_wiki_cli", lambda root, arguments: observed.extend(arguments) or {"ok": True})

    actions.apply_feedback(
        tmp_path,
        feedback_type=feedback_type,
        task_id="task-1",
        entry_id="entry-1",
        occurred_at="2026-07-22T10:00:00+08:00",
    )

    assert observed[0] == expected
    assert observed[observed.index("--task-id") + 1] == "task-1"


def test_outcome_feedback_preserves_correction_fields(monkeypatch, tmp_path):
    observed = []
    monkeypatch.setattr(actions, "_run_wiki_cli", lambda root, arguments: observed.extend(arguments) or {"ok": True})

    actions.apply_feedback(
        tmp_path,
        feedback_type="outcome",
        task_id="task-1",
        entry_id="entry-1",
        occurred_at="2026-07-22",
        outcome="needs_correction",
        explanation="Boundary was too broad",
        corrected_claim="Use this only for repeatable tasks",
    )

    assert observed[0] == "feedback-outcome"
    assert "needs_correction" in observed
    assert "Use this only for repeatable tasks" in observed
