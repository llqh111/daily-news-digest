"""Conservative persistent event archive for the daily digest."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import ACTIVE_EVENT_DAYS, EVENTS_FILE, TZ

log = logging.getLogger(__name__)


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", text.lower())


def _event_id(title: str, day: str) -> str:
    digest = hashlib.sha1(_normal(title).encode("utf-8")).hexdigest()[:12]
    return f"evt_{day.replace('-', '')}_{digest}"


def load_events(path: str = EVENTS_FILE) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload.get("events"), list):
            return payload
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {"version": 1, "events": []}


def _save_events(payload: dict, path: str = EVENTS_FILE) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def _match_existing(article: dict, events: list[dict]) -> dict | None:
    progress = article.get("progress_of") or {}
    previous = _normal(progress.get("prev_zh", ""))
    for event in events:
        if previous and previous in {_normal(event.get("latest_title", "")), _normal(event.get("title", ""))}:
            return event
        if _normal(article.get("title", "")) == _normal(event.get("latest_raw_title", "")):
            return event
    return None


def _facts(article: dict) -> list[str]:
    return [f.get("text", "") for f in article.get("evidence_card", {}).get("confirmed_facts", [])[:3] if f.get("text")]


def _unknowns(article: dict) -> list[str]:
    return list(article.get("evidence_card", {}).get("unknowns", []))[:3]


def update_event_archive(
    articles: list[dict], now: datetime | None = None, path: str = EVENTS_FILE, *, persist: bool = True
) -> dict:
    """Attach a conservative event record to articles and persist the archive.

    Matching deliberately trusts an earlier linkage hit or exact prior headline only.
    A merely similar headline starts a new event, which is safer than a false merge.
    """
    now = now or datetime.now(TZ)
    day = now.strftime("%Y-%m-%d")
    payload = load_events(path)
    events = payload["events"]
    for article in articles:
        event = _match_existing(article, events)
        is_update = event is not None
        if event is None:
            event = {
                "id": _event_id(article.get("title", "event"), day),
                "title": article.get("zh") or article.get("title", ""),
                "first_seen": day,
                "updates": [],
            }
            events.append(event)
        card = article.get("evidence_card", {})
        update = {
            "date": day,
            "article_id": article.get("article_id", ""),
            "title": article.get("zh") or article.get("title", ""),
            "raw_title": article.get("title", ""),
            "why_relevant": article.get("ai_reason", ""),
            "facts": _facts(article),
            "open_questions": _unknowns(article),
            "sources": [s.get("url", "") for s in card.get("sources", []) if s.get("url")],
            "evidence_path": f"digests/meta/{day}-{'AM' if 4 <= now.hour < 16 else 'PM'}-evidence.json",
        }
        if not any(u.get("article_id") == update["article_id"] for u in event["updates"]):
            event["updates"].append(update)
        event.update({
            "latest_seen": day,
            "latest_title": update["title"],
            "latest_raw_title": update["raw_title"],
            "status": "更新中",
            "key_facts": update["facts"],
            "open_questions": update["open_questions"],
            "sources": update["sources"],
        })
        article["event"] = {
            "id": event["id"], "title": event["title"], "is_update": is_update,
            "first_seen": event["first_seen"], "latest_seen": day,
            "open_questions": event["open_questions"],
        }
    payload["updated_at"] = now.isoformat()
    if persist:
        _save_events(payload, path)
    return payload


def save_event_archive(payload: dict, path: str = EVENTS_FILE) -> None:
    _save_events(payload, path)


def active_events(payload: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(TZ)
    cutoff = (now - timedelta(days=ACTIVE_EVENT_DAYS)).strftime("%Y-%m-%d")
    return [event for event in payload.get("events", []) if event.get("latest_seen", "") >= cutoff]


def render_active_events(payload: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    lines = ["# 活跃事件总览", "", f"> 最近 {ACTIVE_EVENT_DAYS} 天仍有新进展的新闻主线。更新于 {now.strftime('%Y-%m-%d %H:%M')}。", ""]
    for event in sorted(active_events(payload, now), key=lambda item: item.get("latest_seen", ""), reverse=True):
        lines.extend([f"## {event['title']}", f"- 状态：{event.get('status', '更新中')}；首见 {event.get('first_seen')}；最近更新 {event.get('latest_seen')}"])
        if event.get("key_facts"):
            lines.append(f"- 最新事实：{event['key_facts'][0]}")
        if event.get("open_questions"):
            lines.append(f"- 仍待观察：{'；'.join(event['open_questions'])}")
        lines.append(f"- 事件 ID：`{event['id']}`")
        lines.append("")
    if len(lines) == 3:
        lines.append("暂无活跃事件。")
    return "\n".join(lines) + "\n"


def save_active_events_overview(payload: dict, now: datetime | None = None, path: str = EVENTS_FILE) -> None:
    overview = Path(path).with_name("ACTIVE_EVENTS.md")
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(render_active_events(payload, now), encoding="utf-8")
