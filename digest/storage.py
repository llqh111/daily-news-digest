"""持久化层：跨天去重记录、当日 session 去重、最终简报存档。

文件读写都收敛在这里——业务模块（fetch / ai）不直接碰 json/磁盘。
所有函数对异常都"读不到就当无历史"——绝不因持久化失败拖崩主流程。
"""

from __future__ import annotations

import os
import json
import logging
import re
from datetime import datetime, date, timedelta

from .config import TZ, SENT_LOG_FILE, SENT_RETENTION_DAYS

log = logging.getLogger(__name__)


def load_sent_links() -> set[str]:
    """加载跨天推送过的文章链接（合并最近 N 天所有记录），用来跨天去重。"""
    if not os.path.exists(SENT_LOG_FILE):
        return set()
    try:
        with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # ── 兼容旧格式（单次 links 列表，无 history）──
        if "links" in data and "history" not in data:
            links = set(data.get("links", []))
            ts = data.get("ts", "unknown")
            log.info(f"已加载旧格式推送记录：{len(links)} 条（{ts}），下次将自动迁移为新格式")
            return links

        # ── 新格式：按天分桶，合并所有天 → 一个 flat set ──
        history = data.get("history", {})
        all_links: set[str] = set()
        for day, links in history.items():
            all_links.update(links)
            log.debug(f"  加载 {day}: {len(links)} 条")
        log.info(f"已加载跨天推送记录：{len(all_links)} 条（{len(history)} 天窗口）")
        return all_links
    except Exception as e:
        log.warning(f"加载推送记录失败，将按无历史处理: {e}")
        return set()


def save_sent_links(links: list[str]) -> None:
    """保存本次候选文章链接，按天归档，保留最近 N 天，自动清理旧天。"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")

    # ── 加载现有数据（兼容旧格式）──
    data: dict = {}
    if os.path.exists(SENT_LOG_FILE):
        try:
            with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    # ── 迁移旧格式 ──
    if "links" in data and "history" not in data:
        old_ts = data.get("ts", "unknown")
        # 尝试从 ts 中提取日期（格式: "2026-06-07 13:46"）
        old_day = old_ts[:10] if len(old_ts) >= 10 else "unknown"
        data = {"history": {old_day: data["links"]}}
        log.info(f"已从旧格式迁移：{len(data['history'][old_day])} 条 → {old_day}")

    history = data.get("history", {})

    # ── 写入今天的链接（去重后存）──
    existing = set(history.get(today, []))
    existing.update(links)
    history[today] = list(existing)

    # ── 清理超过保留天数的旧记录 ──
    cutoff = date.today() - timedelta(days=SENT_RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed_days = []
    for day in list(history.keys()):
        if day < cutoff_str:
            removed_days.append(day)
            del history[day]
    if removed_days:
        log.info(f"清理过期记录：{', '.join(sorted(removed_days))}（保留 {SENT_RETENTION_DAYS} 天窗口）")

    # ── 写回 ──
    now = datetime.now(TZ)
    # 每天 04:00 - 15:59 视为 AM (早班)，16:00 - 03:59 视为 PM (晚班)
    current_session = "AM" if 4 <= now.hour < 16 else "PM"
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "retention_days": SENT_RETENTION_DAYS,
        "last_session": current_session,
        "last_push_timestamp": now.timestamp(),
        "history": history,
    }
    try:
        with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        total = sum(len(v) for v in history.values())
        log.info(f"已保存跨天推送记录：{len(links)} 条（今日），总计 {total} 条 / {len(history)} 天")
    except Exception as e:
        log.warning(f"保存推送记录失败（不影响推送）: {e}")


def _logical_date(dt: datetime) -> str:
    """把凌晨 0-3 点归到前一天：PM 班次跨午夜时，逻辑日期保持不变。"""
    if dt.hour < 4:
        return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def should_skip_session() -> bool:
    """检查当前逻辑班次（AM/PM × 逻辑日期）是否已推送过。

    修复历史 bug：当 GitHub cron 延迟导致 PM 班次在凌晨才执行时，
    save_sent_links 会把日期写成次日，导致次日所有触发误判"PM 已发"而跳过。
    现在改用 last_push_timestamp 推算逻辑日期，彻底避免跨午夜错位。
    """
    now = datetime.now(TZ)
    current_session = "AM" if 4 <= now.hour < 16 else "PM"
    current_logical_date = _logical_date(now)

    if not os.path.exists(SENT_LOG_FILE):
        return False

    try:
        with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        last_ts = data.get("last_push_timestamp")
        if last_ts is None:
            return False

        # 3 小时防连发护栏（应对同一次 cron 积压多个触发）
        if now.timestamp() - last_ts < 3 * 3600:
            log.info("⏭️ 距离上次推送不足 3 小时，跳过以防重复打扰。")
            return True

        # 用 timestamp 还原上次推送的逻辑日期 + 班次
        last_dt = datetime.fromtimestamp(last_ts, tz=TZ)
        last_session = "AM" if 4 <= last_dt.hour < 16 else "PM"
        last_logical_date = _logical_date(last_dt)

        if last_logical_date == current_logical_date and last_session == current_session:
            log.info(
                f"⏭️ 今天 {current_session} 时段已推送过"
                f"（逻辑日期 {last_logical_date} {last_dt.strftime('%H:%M')}），跳过"
            )
            return True

        return False
    except Exception:
        return False


def load_recent_digests() -> str:
    """读取最近的简报标题用于注入跨天上下文。"""
    if not os.path.exists("digests"):
        return ""
    files = sorted([f for f in os.listdir("digests") if f.endswith(".md")], reverse=True)[:3]
    if not files:
        return ""

    events = []
    for fname in reversed(files):  # 按时间正序
        date_str = fname[:10]
        with open(os.path.join("digests", fname), "r", encoding="utf-8") as f:
            content = f.read()
        titles = re.findall(r'\*\*[🔥⭐🎯📈].*?\*\*', content)
        for t in titles:
            events.append(f"- [{date_str}] {t.replace('**', '').strip()}")

    if not events:
        return ""
    return "## 近期已报道事件（用于判断\"进展\"，勿当新事件重写）\n" + "\n".join(events)


def save_digest_markdown(content: str) -> None:
    """保存最终的 Markdown 简报，用于跨天上下文注入。"""
    os.makedirs("digests", exist_ok=True)
    now = datetime.now(TZ)
    session = "AM" if 4 <= now.hour < 16 else "PM"
    filename = now.strftime(f"%Y-%m-%d-{session}.md")
    path = os.path.join("digests", filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
