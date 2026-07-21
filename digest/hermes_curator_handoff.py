"""Create idempotent, confirmation-only handoffs from AI Sentinel to Curator."""

from __future__ import annotations

import argparse
import hashlib
import json
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
        "schema_version": 2,
        "handoff_id": f"curator-{fingerprint}",
        "idempotency_key": f"handoff:{fingerprint}",
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
        should_write = int(existing.get("schema_version", 0) or 0) < 2
        if existing.get("created_at"):
            handoff["created_at"] = existing["created_at"]
            handoff["history"][0]["at"] = existing["created_at"]
    if should_write:
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
    parser.add_argument("item_id")
    parser.add_argument("--daily-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        output = create_handoff(args.daily_root.resolve(), args.item_id)
    except HandoffError as exc:
        print(f"HERMES_CURATOR_HANDOFF_SKIPPED: {exc}")
        return 2
    print(f"HERMES_CURATOR_HANDOFF_OK: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
