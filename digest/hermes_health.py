"""Read-only health checks for the local Hermes bot system."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from collections import Counter


PROFILES = ("reviewcoach", "aisentinel", "knowledgecurator", "studyplanner", "aitutor")


def _hidden_subprocess_kwargs() -> dict[str, int]:
    """Suppress the brief console flash from Windows health probes."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {value}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                **_hidden_subprocess_kwargs(),
            )
            return f'"{value}"' in result.stdout
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return False


def collect_health(
    hermes_home: Path,
    daily_root: Path,
    youtube_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    issues: list[dict[str, str]] = []
    profiles: dict[str, Any] = {}
    for name in PROFILES:
        root = hermes_home / "profiles" / name
        state = _read_json(root / "gateway_state.json")
        log_path = root / "logs" / "gateway.log"
        try:
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-200_000:]
        except OSError:
            log_tail = ""
        connected = "Connected in websocket mode" in log_tail
        running = _pid_alive(state.get("pid"))
        cron = _read_json(root / "cron" / "jobs.json")
        failed_jobs = [
            str(job.get("name") or job.get("id"))
            for job in cron.get("jobs", [])
            if isinstance(job, dict) and (job.get("last_status") == "failed" or job.get("last_error") or job.get("last_delivery_error"))
        ]
        profiles[name] = {"running": running, "connected": connected, "failed_cron_jobs": failed_jobs}
        if not running:
            issues.append({"code": "gateway_down", "source": name, "detail": "网关进程未运行"})
        elif not connected:
            issues.append({"code": "feishu_disconnected", "source": name, "detail": "日志中没有 websocket 已连接证据"})
        for job in failed_jobs:
            issues.append({"code": "cron_failed", "source": name, "detail": job})

    bundle_path = daily_root / "digests" / "hermes" / "ai-intelligence-latest.json"
    bundle = _read_json(bundle_path)
    sources = bundle.get("sources") if isinstance(bundle.get("sources"), dict) else {}
    source_status: dict[str, str] = {}
    for name in ("daily_news_digest", "youtube_digest", "llm_wiki"):
        value = sources.get(name) if isinstance(sources, dict) else None
        status = str(value.get("status", "missing")) if isinstance(value, dict) else "missing"
        source_status[name] = status
        if status.lower() not in {"ok", "success", "available", "present", "previous_night", "context_present"}:
            issues.append({"code": "source_unavailable", "source": name, "detail": status})

    latest_path = youtube_root / "runs" / "latest_run.txt"
    try:
        run_dir = Path(latest_path.read_text(encoding="utf-8-sig").strip())
        summary = (run_dir / "summary.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        run_dir, summary = Path(), ""
    match = re.search(r"Pipeline Run Summary:\s*([A-Za-z]+)", summary, re.IGNORECASE)
    youtube_status = match.group(1).upper() if match else "MISSING"
    if youtube_status not in {"OK", "WARNING", "RUNNING"}:
        issues.append({"code": "youtube_pipeline", "source": "youtube_digest", "detail": youtube_status})
    if run_dir and run_dir.exists():
        age = now - datetime.fromtimestamp(run_dir.stat().st_mtime, tz=now.tzinfo)
        if age > timedelta(hours=36):
            issues.append({"code": "youtube_stale", "source": "youtube_digest", "detail": f"最新运行已超过 {int(age.total_seconds() // 3600)} 小时"})

    return {
        "schema_version": 1,
        "checked_at": now.isoformat(),
        "status": "OK" if not issues else "WARNING",
        "profiles": profiles,
        "sources": source_status,
        "youtube_pipeline": youtube_status,
        "issues": issues,
    }


def render_watchdog(report: dict[str, Any], state_path: Path) -> str:
    """Return text only when the issue set changes; empty output means silence."""
    signature = json.dumps(report.get("issues", []), ensure_ascii=False, sort_keys=True)
    old = _read_json(state_path)
    previous = str(old.get("signature", ""))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"signature": signature, "status": report["status"], "checked_at": report["checked_at"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    if signature == previous:
        return ""
    if not report.get("issues"):
        return "[恢复] Hermes Bot 体系已恢复正常。"
    lines = ["[异常] Hermes Bot 体系发现异常："]
    lines.extend(f"- {item['source']}：{item['detail']}（{item['code']}）" for item in report["issues"])
    return "\n".join(lines)


def render_usage_report(hermes_home: Path, now: datetime | None = None, days: int = 7) -> str:
    """Summarize privacy-minimal card actions; return empty text when unused."""
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=days)
    rows: list[str] = []
    for profile in PROFILES:
        counts: Counter[str] = Counter()
        for path in (hermes_home / "profiles" / profile / "metrics" / "card-actions").glob("*.json"):
            event = _read_json(path)
            try:
                created = datetime.fromisoformat(str(event.get("created_at", "")))
            except ValueError:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=now.tzinfo)
            if created >= cutoff and event.get("bot_action"):
                counts[str(event["bot_action"])] += 1
        if counts:
            details = "、".join(f"{action} {count} 次" for action, count in counts.most_common())
            rows.append(f"- {profile}：{details}")
    if not rows:
        return ""
    return "\n".join([f"Bot 使用效果周报（最近 {days} 天真实卡片操作）", *rows, "说明：只统计按钮事件，不读取聊天正文。"])
