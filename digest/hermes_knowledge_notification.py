"""Render and deliver llm-wiki Knowledge Cycle proposals through Hermes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class KnowledgeNotificationError(RuntimeError):
    """Raised when a Knowledge Cycle artifact cannot be safely delivered."""


Sender = Callable[[str, str, str], None]


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeNotificationError(f"Knowledge run artifact is unavailable: {path}") from exc
    if not isinstance(value, dict):
        raise KnowledgeNotificationError("Knowledge run artifact must be a JSON object")
    return value


def _validated_run(path: Path) -> dict[str, Any]:
    value = _read_object(path)
    if value.get("schema_version") != 1:
        raise KnowledgeNotificationError("Unsupported Knowledge Cycle run schema_version")
    run_id = str(value.get("run_id", "")).strip()
    entries = value.get("promotion_batch")
    if not run_id or not isinstance(entries, list):
        raise KnowledgeNotificationError("Knowledge run artifact lacks run_id or promotion_batch")
    if len(entries) > 5:
        raise KnowledgeNotificationError("Knowledge run artifact exceeds the five-proposal limit")
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            str(entry.get(field, "")).strip()
            for field in ("entry_id", "claim", "when_to_use", "when_not_to_use")
        ):
            raise KnowledgeNotificationError("Knowledge proposal is incomplete")
        evidence = entry.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or not any(isinstance(item, dict) and str(item.get("url", "")).strip() for item in evidence)
        ):
            raise KnowledgeNotificationError("Knowledge proposal lacks traceable evidence")
    return value


def render_notification(run: dict[str, Any]) -> str:
    """Render at most five proposals without treating them as confirmed knowledge."""
    entries = run["promotion_batch"]
    lines = [
        "# 知识晋升提案",
        "",
        f"运行：`{run['run_id']}` · 本轮 {len(entries)} 条",
        "",
        "以下都是待研读的临时判断，不代表已经确认写入长期知识。",
    ]
    for index, entry in enumerate(entries, 1):
        evidence = entry["evidence"][0]
        evidence_title = str(evidence.get("title", "原始证据")).strip() or "原始证据"
        evidence_url = str(evidence.get("url", "")).strip()
        topic = str(entry.get("topic_page", "")).strip() or "尚未指定主题页"
        evidence_line = f"[{evidence_title}]({evidence_url})" if evidence_url else evidence_title
        lines.extend(
            [
                "",
                f"## {index}. {entry['claim']}",
                f"- 适用：{entry['when_to_use']}",
                f"- 不适用：{entry['when_not_to_use']}",
                f"- 主题页：`{topic}`",
                f"- 证据：{evidence_line}",
                f"- 研读编号：`{entry['entry_id']}`",
            ]
        )
    lines.extend(
        [
            "",
            "回复“研读第 N 条”可交给知识库馔员讨论；讨论不会自动确认或改写知识库。",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "deliveries": {}}
    value = _read_object(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("deliveries"), dict):
        raise KnowledgeNotificationError("Knowledge notification ledger has an invalid schema")
    return value


def _save_ledger(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _resolve_feishu_target(profile: str) -> str:
    """Reuse the profile's one existing Feishu destination without duplicating IDs."""
    profile_root = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "profiles" / profile
    env_path = profile_root / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("FEISHU_HOME_CHANNEL="):
                channel = line.split("=", 1)[1].strip()
                if channel:
                    return f"feishu:{channel}"

    jobs_path = profile_root / "cron" / "jobs.json"
    if jobs_path.is_file():
        try:
            payload = json.loads(jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeNotificationError("Hermes profile cron delivery config is unreadable") from exc
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        targets = {
            str(job.get("deliver", "")).strip()
            for job in jobs
            if isinstance(job, dict)
            and job.get("enabled", False)
            and str(job.get("deliver", "")).strip().startswith("feishu:")
        }
        if len(targets) == 1:
            return targets.pop()
    raise KnowledgeNotificationError(
        "No unique Feishu home target is configured for the Hermes profile"
    )


def _hermes_sender(message: str, profile: str, target: str) -> None:
    if target == "auto":
        target = _resolve_feishu_target(profile)
    # Official Hermes scripting contract and target syntax:
    # https://hermes-agent.nousresearch.com/docs/guides/pipe-script-output
    completed = subprocess.run(
        ["hermes", "--profile", profile, "send", "--to", target, "--quiet"],
        input=message,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise KnowledgeNotificationError(
            f"Hermes delivery failed with exit code {completed.returncode}: {detail[:300]}"
        )


def deliver_notification(
    artifact: Path,
    ledger_path: Path,
    *,
    profile: str = "aisentinel",
    target: str = "feishu",
    sender: Sender = _hermes_sender,
) -> dict[str, Any]:
    """Deliver once per run_id; a valid zero-item run is recorded without a push."""
    run = _validated_run(artifact)
    run_id = run["run_id"]
    ledger = _load_ledger(ledger_path)
    existing = ledger["deliveries"].get(run_id)
    if isinstance(existing, dict) and existing.get("status") in {"delivered", "empty"}:
        return {"run_id": run_id, "status": "duplicate", "count": existing.get("count", 0)}

    entries = run["promotion_batch"]
    status = "empty"
    digest = ""
    if entries:
        message = render_notification(run)
        sender(message, profile, target)
        status = "delivered"
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()

    ledger["deliveries"][run_id] = {
        "status": status,
        "count": len(entries),
        "artifact": str(artifact.resolve()),
        "message_sha256": digest,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_ledger(ledger_path, ledger)
    return {"run_id": run_id, "status": status, "count": len(entries)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Deliver one Knowledge Cycle proposal batch to Feishu.")
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--profile", default="aisentinel")
    parser.add_argument("--target", default="auto")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    try:
        run = _validated_run(args.artifact)
        if args.render_only:
            print(render_notification(run) if run["promotion_batch"] else "KNOWLEDGE_NOTIFICATION_EMPTY")
            return 0
        result = deliver_notification(
            args.artifact.resolve(),
            args.ledger.resolve(),
            profile=args.profile,
            target=args.target,
        )
    except KnowledgeNotificationError as exc:
        print(f"KNOWLEDGE_NOTIFICATION_FAILED: {exc}")
        return 1
    print(f"KNOWLEDGE_NOTIFICATION_{result['status'].upper()}: {json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
