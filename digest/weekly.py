"""Weekly review built only from complete, delivered daily artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .config import EVENTS_FILE, TZ
from .events import active_events, load_events


def collect_weekly_items(root: Path, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(TZ)
    try:
        ledger = json.loads((root / "sent_articles.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    items: list[dict] = []
    for run_key, run in ledger.get("delivery_runs", {}).items():
        if not cutoff <= run_key[:10] <= now.strftime("%Y-%m-%d") or run.get("artifact_bundle_status") != "present" or not run.get("delivered_at"):
            continue
        evidence_path = root / "digests" / "meta" / f"{run_key}-evidence.json"
        digest_path = root / "digests" / f"{run_key}.md"
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            quality = json.loads((root / "digests" / "quality" / f"{run_key}.json").read_text(encoding="utf-8"))
            if not isinstance(quality, dict) or not quality:
                continue
            if not digest_path.exists():
                continue
            for item in payload.get("items", []):
                item["run_key"] = run_key
                items.append(item)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return items


def render_weekly_review(items: list[dict], events_payload: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    lines = [f"# 一周变化复盘｜截至 {now.strftime('%Y-%m-%d')}", ""]
    if not items:
        return "\n".join(lines + ["本周没有同时具备已送达、证据卡和质量报告的日报档案，因此不做推断。", ""])
    lines.extend(["## 本周真正变化", ""])
    weekly_events = [e for e in active_events(events_payload, now) if e.get("latest_seen", "") >= (now - timedelta(days=7)).strftime("%Y-%m-%d")]
    for event in sorted(weekly_events, key=lambda e: e.get("latest_seen", ""), reverse=True)[:5]:
        lines.append(f"- **{event['title']}**：{event.get('latest_title', '')}；下一步观察：{'；'.join(event.get('open_questions', []) or ['后续是否出现可验证进展'])}")
    if not weekly_events:
        lines.append("- 本周尚无可确认的跨期事件变化。")
    lines.extend(["", "## 本周值得回看", ""])
    for item in sorted(items, key=lambda i: i.get("relevance_score", 0), reverse=True)[:5]:
        lines.append(f"- **{item.get('headline', '未命名条目')}**：{item.get('why_relevant') or '本周高相关度报道'}（{item.get('run_key')}）")
    lines.extend(["", "## 尚未兑现或仍有分歧", ""])
    uncertain = [i for i in items if not i.get("coverage", {}).get("has_fulltext") or i.get("unknowns")]
    for item in uncertain[:5]:
        reason = "；".join(item.get("unknowns", []) or ["正文材料不足，细节待核验"])
        lines.append(f"- {item.get('headline', '未命名条目')}：{reason}")
    if not uncertain:
        lines.append("- 本周存档中没有需要额外标记的材料缺口。")
    lines.extend(["", "## 下周观察点", ""])
    for event in weekly_events[:5]:
        lines.append(f"- {event['title']}：{'；'.join(event.get('open_questions', []) or ['关注是否有新的可验证事实'])}")
    return "\n".join(lines) + "\n"


def build_weekly_review(root: Path, now: datetime | None = None) -> str:
    return render_weekly_review(collect_weekly_items(root, now), load_events(str(root / "digests" / "events.json")), now)


def save_weekly_review(root: Path, content: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(TZ)
    target = root / "digests" / "weekly" / f"{now.strftime('%G-W%V')}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
