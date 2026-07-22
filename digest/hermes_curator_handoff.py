"""Create idempotent, confirmation-only handoffs from AI Sentinel to Curator."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class HandoffError(RuntimeError):
    """Raised when a requested intelligence item is not safe to hand off."""


def _load_bundle(root: Path) -> dict[str, Any]:
    path = root / "digests" / "hermes" / "ai-intelligence-latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError("AI intelligence bundle is unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise HandoffError("AI intelligence bundle has an invalid schema")
    return payload


def create_handoff(daily_root: Path, item_id: str, *, source_bot: str = "aisentinel", action: str = "curate") -> Path:
    """Create a preview artifact only; this function never writes to llm-wiki."""
    bundle = _load_bundle(daily_root)
    item = next((value for value in bundle["items"] if isinstance(value, dict) and value.get("article_id") == item_id), None)
    if item is None:
        raise HandoffError(f"Intelligence item not found: {item_id}")
    if not item.get("url") or not item.get("evidence_path"):
        raise HandoffError("Intelligence item lacks a traceable source")

    fingerprint = hashlib.sha256(f"{bundle.get('date', '')}:{item_id}:{item['url']}".encode("utf-8")).hexdigest()[:16]
    created_at = datetime.now(timezone.utc).isoformat()
    handoff = {
        "schema_version": 3,
        "handoff_id": f"curator-{fingerprint}",
        "idempotency_key": f"handoff:{fingerprint}",
        "source_type": "intelligence_item",
        "source_bot": source_bot,
        "target_bot": "knowledgecurator",
        "action": action,
        "status": "pending",
        "bundle_date": bundle.get("date", ""),
        "created_at": created_at,
        "confirmed_at": None,
        "consumed_at": None,
        "source_item": item,
        "history": [{"status": "pending", "at": created_at, "actor": source_bot}],
        "allowed_next_action": "Create a routing preview only. Do not write into llm-wiki until the user explicitly confirms this handoff_id.",
    }
    output_dir = daily_root / "digests" / "hermes" / "curator-handoffs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{handoff['handoff_id']}.json"
    temporary = output.with_suffix(".json.tmp")
    should_write = True
    if output.exists():
        existing = _load_existing_handoff(output)
        should_write = int(existing.get("schema_version", 0) or 0) < 3
        if existing.get("created_at"):
            handoff["created_at"] = existing["created_at"]
            handoff["history"][0]["at"] = existing["created_at"]
    if should_write:
        temporary.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output)
    return output


def _load_knowledge_run(wiki_root: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise HandoffError("Knowledge run_id is not safe")
    path = wiki_root / "reports" / "AI" / "runs" / f"{run_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError(f"Knowledge run is unavailable: {run_id}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("run_id") != run_id
        or not isinstance(payload.get("promotion_batch"), list)
    ):
        raise HandoffError("Knowledge run has an invalid schema")
    return path, payload


def create_knowledge_promotion_handoff(
    daily_root: Path,
    wiki_root: Path,
    run_id: str,
    entry_id: str,
    *,
    source_bot: str = "aisentinel",
) -> Path:
    """Create a v3 review handoff for one provisional Knowledge Entry."""
    run_path, payload = _load_knowledge_run(wiki_root, run_id)
    entry = next(
        (
            value
            for value in payload["promotion_batch"]
            if isinstance(value, dict) and value.get("entry_id") == entry_id
        ),
        None,
    )
    if entry is None:
        raise HandoffError(f"Knowledge Entry not found in run: {entry_id}")
    evidence = entry.get("evidence")
    if not all(
        str(entry.get(field, "")).strip()
        for field in ("claim", "when_to_use", "when_not_to_use")
    ) or not isinstance(evidence, list) or not evidence or not any(
        isinstance(item, dict) and str(item.get("url", "")).strip() for item in evidence
    ):
        raise HandoffError("Knowledge Entry lacks claim boundaries or traceable evidence")

    fingerprint = hashlib.sha256(f"{run_id}:{entry_id}".encode("utf-8")).hexdigest()[:16]
    created_at = datetime.now(timezone.utc).isoformat()
    handoff = {
        "schema_version": 3,
        "handoff_id": f"knowledge-{fingerprint}",
        "idempotency_key": f"knowledge-promotion:{run_id}:{entry_id}",
        "source_type": "knowledge_promotion",
        "source_bot": source_bot,
        "target_bot": "knowledgecurator",
        "action": "review",
        "status": "pending",
        "run_id": run_id,
        "entry_id": entry_id,
        "claim": entry["claim"],
        "when_to_use": entry["when_to_use"],
        "when_not_to_use": entry["when_not_to_use"],
        "evidence": evidence,
        "topic_page": entry.get("topic_page"),
        "production_writes_enabled": bool(payload.get("production_writes_enabled", False)),
        "source_artifact": str(run_path.resolve()),
        "created_at": created_at,
        "confirmed_at": None,
        "consumed_at": None,
        "history": [{"status": "pending", "at": created_at, "actor": source_bot}],
        "allowed_next_actions": ["confirm", "revise", "withdraw"],
        "allowed_next_action": "Discuss this provisional entry only. Apply review or usage feedback only after explicit user confirmation.",
    }
    output_dir = daily_root / "digests" / "hermes" / "curator-handoffs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{handoff['handoff_id']}.json"
    if output.exists():
        existing = _load_existing_handoff(output)
        if existing.get("schema_version") == 3:
            return output
        if existing.get("created_at"):
            handoff["created_at"] = existing["created_at"]
            handoff["history"][0]["at"] = existing["created_at"]
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return output


def _load_existing_handoff(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a confirmation-only AI Sentinel to Curator handoff.")
    parser.add_argument("item_id", nargs="?")
    parser.add_argument("--daily-root", type=Path, default=Path.cwd())
    parser.add_argument("--wiki-root", type=Path, default=Path(r"D:\Documents\llm-wiki"))
    parser.add_argument("--knowledge-run")
    parser.add_argument("--entry-id")
    args = parser.parse_args()
    try:
        if args.knowledge_run or args.entry_id:
            if not args.knowledge_run or not args.entry_id or args.item_id:
                parser.error("--knowledge-run and --entry-id must be used together without item_id")
            output = create_knowledge_promotion_handoff(
                args.daily_root.resolve(),
                args.wiki_root.resolve(),
                args.knowledge_run,
                args.entry_id,
            )
        elif args.item_id:
            output = create_handoff(args.daily_root.resolve(), args.item_id)
        else:
            parser.error("provide item_id or --knowledge-run with --entry-id")
    except HandoffError as exc:
        print(f"HERMES_CURATOR_HANDOFF_SKIPPED: {exc}")
        return 2
    print(f"HERMES_CURATOR_HANDOFF_OK: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
