"""Export verified digest runs for the read-only Hermes AI sentinel.

The exporter deliberately consumes only a delivery run that was successfully
sent and has both evidence and quality artifacts.  Hermes must never present
an unfinished or stale draft as current news.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class HermesExportError(RuntimeError):
    """Raised when a digest run is not safe for Hermes to consume."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HermesExportError(f"无法读取 JSON 文件：{path.name}") from exc
    if not isinstance(value, dict):
        raise HermesExportError(f"JSON 根对象必须是对象：{path.name}")
    return value


def _primary_source(card: dict[str, Any]) -> dict[str, Any]:
    for source in card.get("sources", []):
        if isinstance(source, dict) and source.get("role") == "primary":
            return source
    return {}


def _export_item(card: dict[str, Any]) -> dict[str, Any]:
    source = _primary_source(card)
    return {
        "article_id": card.get("article_id", ""),
        "title": card.get("headline", ""),
        "url": source.get("canonical_url") or source.get("url") or "",
        "publisher": source.get("publisher", ""),
        "summary": card.get("summary", ""),
        "category": card.get("category", ""),
        "published_at": card.get("published_at", ""),
        "why_relevant": card.get("why_relevant", ""),
        "relevance_score": card.get("relevance_score", 0),
        "confirmed_facts": card.get("confirmed_facts", []),
        "entities": card.get("entities", []),
        "numbers": card.get("numbers", []),
        "dates": card.get("dates", []),
        "unknowns": card.get("unknowns", []),
        "coverage": card.get("coverage", {}),
        "evidence_complete": bool(card.get("confirmed_facts")) and bool(source.get("canonical_url") or source.get("url")),
    }


def export_run(root: Path, run_key: str) -> Path:
    """Validate and export one successfully delivered digest run.

    ``root`` is the daily-news-digest repository.  The returned file is both
    immutable per run and copied atomically to ``digests/hermes/latest.json``.
    """
    ledger = _load_json(root / "sent_articles.json")
    run = ledger.get("delivery_runs", {}).get(run_key)
    if not isinstance(run, dict):
        raise HermesExportError(f"未找到已送达批次：{run_key}")
    if run.get("artifact_bundle_status") != "present":
        raise HermesExportError(f"批次证据或质量文件不完整：{run_key}")

    markdown_path = root / "digests" / f"{run_key}.md"
    evidence_path = root / "digests" / "meta" / f"{run_key}-evidence.json"
    quality_path = root / "digests" / "quality" / f"{run_key}.json"
    if not markdown_path.is_file():
        raise HermesExportError(f"缺少已送达简报：{markdown_path.name}")

    evidence = _load_json(evidence_path)
    quality = _load_json(quality_path)
    items = evidence.get("items")
    if not isinstance(items, list) or not items:
        raise HermesExportError(f"证据文件没有可用新闻条目：{evidence_path.name}")

    exported = {
        "schema_version": SCHEMA_VERSION,
        "source": "daily-news-digest",
        "run_key": run_key,
        "date": evidence.get("date", run_key[:10]),
        "session": evidence.get("session", run_key.rsplit("-", 1)[-1]),
        "delivered_at": run.get("delivered_at", ""),
        "delivery_successful": bool(run.get("delivered_at")),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "quality": {
            "total_items": quality.get("total_items", 0),
            "items_with_unsupported_numbers": quality.get("items_with_unsupported_numbers", 0),
            "unsupported_ratio": quality.get("unsupported_ratio", 0.0),
        },
        "items": [_export_item(item) for item in items if isinstance(item, dict)],
    }

    output_dir = root / "digests" / "hermes"
    output_dir.mkdir(parents=True, exist_ok=True)
    run_output = output_dir / f"{run_key}.json"
    for target in (run_output, output_dir / "latest.json"):
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    return run_output


def main() -> int:
    parser = argparse.ArgumentParser(description="导出经过验证的每日简报给 Hermes")
    parser.add_argument("run_key", help="批次键，例如 2026-07-14-AM")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="daily-news-digest 根目录")
    args = parser.parse_args()
    try:
        output = export_run(args.root.resolve(), args.run_key)
    except HermesExportError as exc:
        print(f"HERMES_EXPORT_SKIPPED: {exc}")
        return 2
    print(f"HERMES_EXPORT_OK: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
