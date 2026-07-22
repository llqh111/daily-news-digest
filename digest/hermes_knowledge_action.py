"""Safely bridge confirmed Hermes actions to llm-wiki's authoritative CLI."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Sequence


class KnowledgeActionError(RuntimeError):
    """Raised when an action cannot be validated or applied."""


def _safe_entry_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
        raise KnowledgeActionError("entry_id is not safe")
    return value


def _valid_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise KnowledgeActionError("date must be YYYY-MM-DD") from exc


def _run_wiki_cli(wiki_root: Path, arguments: Sequence[str]) -> Any:
    completed = subprocess.run(
        [sys.executable, "-m", "_system.tools.knowledge_cycle_run", *arguments],
        cwd=wiki_root,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise KnowledgeActionError(detail or f"llm-wiki exited with {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise KnowledgeActionError("llm-wiki returned invalid JSON") from exc


def apply_review(
    wiki_root: Path,
    *,
    entry_id: str,
    action: str,
    reviewed_on: str,
    claim: str | None = None,
    reason: str | None = None,
) -> Any:
    """Apply one explicitly confirmed review decision."""
    entry_id = _safe_entry_id(entry_id)
    reviewed_on = _valid_date(reviewed_on)
    decision: dict[str, str] = {"entry_id": entry_id, "action": action}
    if action == "revise":
        if not claim or not claim.strip():
            raise KnowledgeActionError("revise requires a non-empty claim")
        decision["claim"] = claim.strip()
    elif action == "withdraw":
        if not reason or not reason.strip():
            raise KnowledgeActionError("withdraw requires a reason")
        decision["reason"] = reason.strip()
    elif action != "confirm":
        raise KnowledgeActionError(f"unsupported review action: {action}")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    ) as handle:
        json.dump([decision], handle, ensure_ascii=False)
        decision_path = Path(handle.name)
    try:
        return _run_wiki_cli(
            wiki_root,
            [
                "review",
                "--root",
                str(wiki_root),
                "--decisions",
                str(decision_path),
                "--date",
                reviewed_on,
            ],
        )
    finally:
        decision_path.unlink(missing_ok=True)


def apply_feedback(
    wiki_root: Path,
    *,
    feedback_type: str,
    task_id: str,
    entry_id: str,
    occurred_at: str,
    outcome: str | None = None,
    explanation: str | None = None,
    corrected_claim: str | None = None,
) -> Any:
    """Record real retrieval, citation, or outcome feedback in llm-wiki."""
    entry_id = _safe_entry_id(entry_id)
    if not task_id.strip():
        raise KnowledgeActionError("task_id must not be empty")
    command = {
        "retrieved": "feedback-retrieved",
        "cited": "feedback-cited",
        "outcome": "feedback-outcome",
    }.get(feedback_type)
    if command is None:
        raise KnowledgeActionError(f"unsupported feedback type: {feedback_type}")
    arguments = [
        command,
        "--root",
        str(wiki_root),
        "--task-id",
        task_id,
        "--entry-id",
        entry_id,
        "--at",
        occurred_at,
    ]
    if feedback_type == "outcome":
        if outcome not in {"useful", "needs_correction", "unused", "obsolete"}:
            raise KnowledgeActionError("outcome feedback requires a supported outcome")
        arguments.extend(["--outcome", outcome])
        if explanation:
            arguments.extend(["--explanation", explanation])
        if corrected_claim:
            arguments.extend(["--corrected-claim", corrected_claim])
    return _run_wiki_cli(wiki_root, arguments)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply confirmed Hermes knowledge actions.")
    parser.add_argument("--wiki-root", type=Path, default=Path(r"D:\Documents\llm-wiki"))
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review")
    review.add_argument("--entry-id", required=True)
    review.add_argument("--action", choices=["confirm", "revise", "withdraw"], required=True)
    review.add_argument("--date", required=True)
    review.add_argument("--claim")
    review.add_argument("--reason")

    feedback = commands.add_parser("feedback")
    feedback.add_argument("--type", choices=["retrieved", "cited", "outcome"], required=True)
    feedback.add_argument("--task-id", required=True)
    feedback.add_argument("--entry-id", required=True)
    feedback.add_argument("--at", required=True)
    feedback.add_argument("--outcome", choices=["useful", "needs_correction", "unused", "obsolete"])
    feedback.add_argument("--explanation")
    feedback.add_argument("--corrected-claim")
    args = parser.parse_args()

    try:
        if args.command == "review":
            result = apply_review(
                args.wiki_root.resolve(),
                entry_id=args.entry_id,
                action=args.action,
                reviewed_on=args.date,
                claim=args.claim,
                reason=args.reason,
            )
        else:
            result = apply_feedback(
                args.wiki_root.resolve(),
                feedback_type=args.type,
                task_id=args.task_id,
                entry_id=args.entry_id,
                occurred_at=args.at,
                outcome=args.outcome,
                explanation=args.explanation,
                corrected_claim=args.corrected_claim,
            )
    except KnowledgeActionError as exc:
        print(f"HERMES_KNOWLEDGE_ACTION_FAILED: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
