"""Build a read-only, traceable AI intelligence bundle for Hermes.

The bundle is deliberately derived from existing artifacts only.  It never
fetches the web and it does not write into llm-wiki.  A Feishu bot can consume
one stable file instead of trying to search three repositories at chat time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_LLM_WIKI_ROOT = Path(r"D:\Documents\llm-wiki")
DEFAULT_YOUTUBE_ROOT = Path(r"D:\Documents\youtube-digest")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _front_matter(path: Path) -> dict[str, str]:
    """Read the small scalar YAML front matter used by llm-wiki RAW files."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def _score(value: str) -> int:
    match = re.match(r"(\d+)", value or "")
    return int(match.group(1)) if match else 0


def _candidate_summary(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    marker = "## One-line judgment"
    if marker not in text:
        return ""
    body = text.split(marker, 1)[1]
    for line in body.splitlines()[1:]:
        line = line.strip()
        if line and not line.startswith("#") and "What is the durable judgment" not in line:
            return line
    return ""


def _daily_items(daily_root: Path, target_date: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    # 12:30 delivery consumes only the already-completed morning batch. A PM
    # batch can never overwrite the news basis for the same day's push.
    path = daily_root / "digests" / "hermes" / f"{target_date}-AM.json"
    if not path.is_file():
        return [], {"status": "missing", "path": str(path)}
    try:
        payload = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], {"status": "invalid", "path": str(path), "detail": str(exc)}
    if payload.get("date") != target_date or payload.get("session") != "AM" or not payload.get("delivery_successful", False):
        return [], {"status": "not_successful_am", "path": str(path), "artifact_date": str(payload.get("date", ""))}
    items = payload.get("items")
    if not isinstance(items, list):
        return [], {"status": "invalid", "path": str(path), "detail": "items is not a list"}
    verified = [
        {
            **item,
            "source_system": "daily-news-digest",
            "evidence_path": str(path),
            "source_artifact_date": target_date,
            "freshness_label": "today",
        }
        for item in items
        if isinstance(item, dict) and item.get("title") and item.get("url")
    ]
    return verified, {"status": "present" if verified else "empty", "path": str(path), "count": str(len(verified))}


def _wiki_items(wiki_root: Path, target_date: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    candidates = wiki_root / "03_review" / "candidates"
    raw_root = wiki_root / "01_raw" / "ai_sources"
    if not candidates.is_dir() or not raw_root.is_dir():
        return [], {"status": "missing", "path": str(candidates)}

    prefix = target_date.replace("-", "") + "-"
    items: list[dict[str, Any]] = []
    for candidate in candidates.rglob("*.md"):
        if not candidate.name.startswith(prefix):
            continue
        meta = _front_matter(candidate)
        if meta.get("status", "pending") not in {"pending", "approved"} or _score(meta.get("score", "")) < 23:
            continue
        source = meta.get("source", "")
        raw = (wiki_root / source).resolve()
        if not source or raw_root.resolve() not in raw.parents or not raw.is_file():
            continue
        raw_meta = _front_matter(raw)
        url = raw_meta.get("url", "")
        title = raw_meta.get("title", "")
        if not url or not title:
            continue
        items.append({
            "article_id": f"llm-wiki:{candidate.relative_to(wiki_root).as_posix()}",
            "title": title,
            "url": url,
            "publisher": raw_meta.get("author") or raw_meta.get("channel") or raw_meta.get("source", ""),
            "confirmed_facts": [],
            "entities": [raw_meta.get("source_type", ""), raw_meta.get("topic", "")],
            "candidate_summary": _candidate_summary(candidate),
            "score": meta.get("score", ""),
            "priority": meta.get("priority", ""),
            "source_system": "llm-wiki",
            "evidence_path": str(candidate),
            "raw_path": str(raw),
            "source_artifact_date": target_date,
            "freshness_label": "today",
        })
    return items, {"status": "present" if items else "empty", "path": str(candidates), "count": str(len(items))}


def _load_radar(path: Path, expected_date: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not path.is_file():
        return [], {"status": "missing", "path": str(path)}
    try:
        payload = _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], {"status": "invalid", "path": str(path), "detail": str(exc)}
    if payload.get("date") != expected_date or payload.get("run_status") != "ok":
        return [], {"status": "not_successful", "path": str(path), "artifact_date": str(payload.get("date", ""))}
    items = [
        {**item, "source_artifact_date": expected_date, "freshness_label": "today"}
        for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("title") and item.get("url")
    ]
    return items, {"status": "present" if items else "empty", "path": str(path), "count": str(len(items))}


def _radar_items(youtube_root: Path, target_date: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Read today's radar, or the successful previous-night run only.

    The full YouTube pipeline runs around 22:00.  A 12:30 brief therefore may
    use the immediately preceding night's successful artifact, but every item
    is labelled with its real artifact date.  We never fall back by more than
    one day or present the overnight result as a same-day collection.
    """
    current_path = youtube_root / "digests" / "hermes" / f"{target_date}.json"
    current_items, current_status = _load_radar(current_path, target_date)
    if current_items:
        return current_items, current_status

    previous_date = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat()
    previous_path = youtube_root / "digests" / "hermes" / f"{previous_date}.json"
    if not previous_path.is_file():
        latest = youtube_root / "digests" / "hermes" / "latest.json"
        previous_path = latest if latest.is_file() else previous_path
    previous_items, previous_status = _load_radar(previous_path, previous_date)
    if previous_items:
        labelled = [{**item, "freshness_label": "previous_night"} for item in previous_items]
        return labelled, {
            "status": "previous_night",
            "path": str(previous_path),
            "artifact_date": previous_date,
            "count": str(len(labelled)),
            "current_status": current_status.get("status", "missing"),
        }
    return [], {
        **current_status,
        "previous_night_status": previous_status.get("status", "missing"),
        "previous_night_path": str(previous_path),
    }


def _priority(item: dict[str, Any]) -> tuple[int, int, int]:
    """Prefer complete evidence, then source quality/relevance, deterministically."""
    evidence = 1 if item.get("evidence_complete") else 0
    quality = 1 if item.get("source_system") == "daily-news-digest" else 0
    score = _score(str(item.get("relevance_score") or item.get("score") or "0"))
    return evidence, quality, score


def _normalized_title(item: dict[str, Any]) -> str:
    return re.sub(r"[^\w]+", "", str(item.get("title", "")).lower())


def _same_title(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a, b = _normalized_title(left), _normalized_title(right)
    return bool(a and b and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.92))


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in sorted(items, key=_priority, reverse=True):
        url = str(item.get("url", "")).strip().lower()
        if not url or any(url == str(existing.get("url", "")).strip().lower() or _same_title(item, existing) for existing in result):
            continue
        result.append(item)
    return result[:5]


def _raw_summary(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    for marker in ("## 机器摘要", "## 摘要", "## Summary"):
        if marker in text:
            body = text.split(marker, 1)[1].split("\n## ", 1)[0]
            return re.sub(r"\s+", " ", body).strip()[:700]
    return ""


def _enrich_with_recent_raw(items: list[dict[str, Any]], wiki_root: Path, target_date: str) -> tuple[list[dict[str, Any]], int]:
    """Attach recent RAW context to news items without promoting RAW to Wiki."""
    raw_root = wiki_root / "01_raw" / "ai_sources"
    if not raw_root.is_dir() or not items:
        return items, 0
    previous_date = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat()
    allowed_dates = {target_date, previous_date}
    contexts: list[dict[str, str]] = []
    for raw in raw_root.rglob("*.md"):
        meta = _front_matter(raw)
        captured = meta.get("captured_at", "")[:10]
        file_date = raw.name[:10]
        if captured not in allowed_dates and file_date not in allowed_dates:
            continue
        if not meta.get("url") or not meta.get("title"):
            continue
        contexts.append({
            "url": meta["url"].strip().lower(),
            "title": meta["title"],
            "raw_path": str(raw),
            "captured_at": meta.get("captured_at", ""),
            "topic": meta.get("topic") or meta.get("subtopic", ""),
            "source_type": meta.get("source_type", ""),
            "summary": _raw_summary(raw),
        })

    matched = 0
    enriched: list[dict[str, Any]] = []
    for item in items:
        url = str(item.get("url", "")).strip().lower()
        context = next((value for value in contexts if value["url"] == url), None)
        if context is None:
            context = next((value for value in contexts if _same_title(item, {"title": value["title"]})), None)
        if context is None:
            enriched.append(item)
            continue
        matched += 1
        enriched.append({
            **item,
            "knowledge_context": {
                key: value for key, value in context.items() if key not in {"url", "title"} and value
            },
        })
    return enriched, matched


def build_bundle(daily_root: Path, *, wiki_root: Path = DEFAULT_LLM_WIKI_ROOT, youtube_root: Path = DEFAULT_YOUTUBE_ROOT, target_date: str | None = None) -> Path:
    """Write an atomic, date-scoped bundle and return its immutable path."""
    target_date = target_date or date.today().isoformat()
    daily, daily_status = _daily_items(daily_root, target_date)
    radar, radar_status = _radar_items(youtube_root, target_date)
    wiki, wiki_status = _wiki_items(wiki_root, target_date)
    items = _dedupe([*daily, *radar, *wiki])
    items, raw_context_count = _enrich_with_recent_raw(items, wiki_root, target_date)
    wiki_status = {
        **wiki_status,
        "candidate_count": wiki_status.get("count", "0"),
        "raw_context_count": str(raw_context_count),
    }
    if raw_context_count and wiki_status.get("status") in {"empty", "missing"}:
        wiki_status["status"] = "context_present"
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "date": target_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {"daily_news_digest": daily_status, "youtube_digest": radar_status, "llm_wiki": wiki_status},
        "items": items,
    }
    output_dir = daily_root / "digests" / "hermes"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"ai-intelligence-{target_date}.json"
    for path in (output, output_dir / "ai-intelligence-latest.json"):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Hermes AI intelligence bundle from verified local artifacts.")
    parser.add_argument("--daily-root", type=Path, default=Path.cwd())
    parser.add_argument("--wiki-root", type=Path, default=Path(os.getenv("HERMES_LLM_WIKI_ROOT", DEFAULT_LLM_WIKI_ROOT)))
    parser.add_argument("--youtube-root", type=Path, default=Path(os.getenv("HERMES_YOUTUBE_DIGEST_ROOT", DEFAULT_YOUTUBE_ROOT)))
    parser.add_argument("--date", dest="target_date")
    args = parser.parse_args()
    output = build_bundle(args.daily_root.resolve(), wiki_root=args.wiki_root.resolve(), youtube_root=args.youtube_root.resolve(), target_date=args.target_date)
    print(f"HERMES_INTELLIGENCE_BUNDLE_OK: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
