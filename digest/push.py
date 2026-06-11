"""推送层：Server酱（微信）、Telegram、失败告警。

设计要点：
· 每个通道独立 3 次指数退避重试 —— 应对 GitHub Actions 海外 runner 偶发掉包
· 返回值契约让主流程能判定「是否至少一个通道送达」（any_delivered）
· 失败告警自己不抛错 —— 告警本身失败也不能再影响主流程

⚠️ 测试兼容性提示：
   test_core.py 中 monkeypatch.setattr("main.TELEGRAM_BOT_TOKEN", "") 已改为
   monkeypatch.setattr("digest.push.TELEGRAM_BOT_TOKEN", "")。因为字符串值
   拷贝，patch main 上的副本不会影响本模块。模块对象（requests / time）是
   sys.modules 单例，patch "main.requests.post" 仍然兼容。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import requests

from .config import TZ

log = logging.getLogger(__name__)

# 模块加载时一次性读取，保持与原 main.py 顶部行为一致
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def push_to_wechat(content: str, sendkeys: list[str]) -> int:
    """通过 Server酱 把内容推送到微信（支持多个 SendKey，每人一个）。

    内置 3 次重试（指数退避），应对 GitHub Actions 海外 runner 连接国内
    Server酱时偶发的 Connection reset / timeout。

    返回成功送达的人数（0 = 全部失败）。调用方据此判断是否真正送达。
    """
    if not sendkeys:
        raise RuntimeError("❌ 没有设置 SERVERCHAN_SENDKEY，请检查 .env 文件")

    RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    MAX_RETRIES = 3

    today_str = datetime.now(TZ).strftime("%m/%d")

    failed = []
    success_count = 0
    for i, key in enumerate(sendkeys):
        key = key.strip()
        if not key:
            continue
        label = f"收件人{i+1}" if len(sendkeys) > 1 else "微信"

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                log.info(f"正在推送到 {label} (Server酱) ... 第 {attempt}/{MAX_RETRIES} 次")
                resp = requests.post(
                    f"https://sctapi.ftqq.com/{key}.send",
                    data={
                        "title": f"📰 每日全球要闻 — {today_str}",
                        "desp": content,
                    },
                    timeout=30,
                )
                result = resp.json()
                if result.get("code") == 0:
                    log.info(f"✅ {label} 推送成功！")
                    success = True
                    break
                else:
                    log.error(f"❌ {label} 推送失败: {result}")
                    failed.append(f"{label}: {result}")
                    break  # 业务错误不重试
            except RETRYABLE as e:
                if attempt == MAX_RETRIES:
                    log.error(f"❌ {label} 推送异常（已重试 {MAX_RETRIES} 次）: {e}")
                    failed.append(f"{label}: {e}")
                else:
                    wait = 5 * (2 ** (attempt - 1))
                    log.warning(
                        f"⚠️ {label} 推送失败（{type(e).__name__}），"
                        f"{wait}s 后重试..."
                    )
                    time.sleep(wait)
            except Exception as e:
                log.error(f"❌ {label} 推送异常（非网络错误，不重试）: {e}")
                failed.append(f"{label}: {e}")
                break

        if success:
            success_count += 1

    if failed:
        log.warning(f"部分推送失败 ({len(failed)}/{len(sendkeys)}): {'; '.join(failed)}")
    else:
        log.info(f"🎉 全部推送成功（共 {len(sendkeys)} 人）")
    return success_count


def push_to_telegram(content: str) -> bool | None:
    """通过 Telegram Bot 推送简报。返回 True=全部成功 / False=有段失败 / None=未配置。

    Telegram 单条消息上限 4096 字符，晨报通常远超此值 → 按段落边界
    自动分段，每段 ≤ 3500 字符（留安全余量）。多段顺序发送，每段
    之间间隔 0.5s 避免被限速。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("📱 Telegram 未配置，跳过")
        return None  # None = 跳过（未配置），区别于 False = 配置了但发送失败

    MAX_CHUNK = 3500  # 留余量给标题行和分段标记
    TG_RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )
    TG_MAX_RETRIES = 3

    # 按双换行（段落边界）切分
    paragraphs = content.split("\n\n")
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 2 <= MAX_CHUNK:
            current = (current + "\n\n" + p) if current else p
        else:
            if current:
                chunks.append(current)
            # 单个段落超限则硬切
            if len(p) > MAX_CHUNK:
                for j in range(0, len(p), MAX_CHUNK):
                    chunks.append(p[j:j + MAX_CHUNK])
                current = ""
            else:
                current = p
    if current:
        chunks.append(current)

    total = len(chunks)
    log.info(f"📱 Telegram 推送（共 {total} 段）...")

    all_ok = True
    for idx, chunk in enumerate(chunks, 1):
        if total > 1:
            header = f"📰 每日全球要闻 ({idx}/{total})\n\n"
        else:
            header = ""
        text = header + chunk

        sent = False
        for attempt in range(1, TG_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": text,
                    },
                    timeout=30,
                )
                result = resp.json()
                if result.get("ok"):
                    log.info(f"✅ Telegram ({idx}/{total}) 推送成功")
                    sent = True
                    break
                else:
                    log.error(
                        f"❌ Telegram ({idx}/{total}) 推送失败: "
                        f"{result.get('description', result)}"
                    )
                    if attempt == TG_MAX_RETRIES:
                        break  # 重试到顶仍失败，放弃该段（由 all_ok 汇总，不再 raise）
            except TG_RETRYABLE as e:
                if attempt == TG_MAX_RETRIES:
                    log.error(f"❌ Telegram ({idx}/{total}) 推送异常: {e}")
                    break  # 不再 raise，交给 all_ok 统一裁决
                wait = 5 * (2 ** (attempt - 1))
                log.warning(
                    f"⚠️ Telegram ({idx}/{total}) 推送失败 "
                    f"（{type(e).__name__}），{wait}s 后重试..."
                )
                time.sleep(wait)

        if not sent:
            all_ok = False

        if idx < total:
            time.sleep(0.5)  # 段间短暂间隔，避免 Telegram 限速

    if all_ok:
        log.info("🎉 Telegram 全部推送成功")
    else:
        log.warning("⚠️ Telegram 有段落推送失败")
    return all_ok


# ═══════════════════════════════════════════════════
#  失败告警：流水线任何环节崩了，推送简短告警
# ═══════════════════════════════════════════════════

def _send_alert_summary(msgs: list[str]) -> str:
    """汇总多条告警发送结果，用于日志。"""
    if not msgs:
        return "✅ 全部告警通道已发送"
    return "部分告警通道失败: " + "; ".join(msgs)


def send_failure_alert(error_msg: str, stage: str = "未知") -> None:
    """流水线失败时，向所有可用通道推送简短告警。

    不会抛异常——告警本身失败了也不影响主流程日志。
    """
    now_str = datetime.now(TZ).strftime("%m/%d %H:%M")
    title = f"⚠️ 每日要闻推送失败 — {now_str}"
    body = (
        f"## ⚠️ 每日全球要闻 — 推送失败\n\n"
        f"**失败环节**: {stage}\n"
        f"**时间**: {now_str}\n\n"
        f"**错误信息**:\n"
        f"```\n{error_msg[:800]}\n```\n\n"
        f"请检查 GitHub Actions 日志：\n"
        f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions\n\n"
        f"---\n"
        f"📡 由 Daily News Digest 失败告警自动发送"
    )

    failed: list[str] = []

    # ── Server酱 ──
    if SERVERCHAN_SENDKEY:
        sendkeys = [k.strip() for k in SERVERCHAN_SENDKEY.split(",") if k.strip()]
        for key in sendkeys:
            try:
                resp = requests.post(
                    f"https://sctapi.ftqq.com/{key}.send",
                    data={"title": title, "desp": body},
                    timeout=15,
                )
                result = resp.json()
                if result.get("code") == 0:
                    log.info(f"✅ 失败告警已通过 Server酱 发送")
                else:
                    failed.append(f"Server酱: {result}")
            except Exception as e:
                failed.append(f"Server酱: {e}")

    # ── Telegram ──
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_text = (
                f"⚠️ <b>每日要闻推送失败</b>\n\n"
                f"<b>失败环节</b>: {stage}\n"
                f"<b>时间</b>: {now_str}\n\n"
                f"<b>错误</b>:\n<pre>{error_msg[:500]}</pre>\n\n"
                f"<a href=\"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions\">查看 Actions 日志</a>"
            )
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": tg_text,
                    "parse_mode": "HTML",
                },
                timeout=15,
            )
            result = resp.json()
            if result.get("ok"):
                log.info(f"✅ 失败告警已通过 Telegram 发送")
            else:
                failed.append(f"Telegram: {result.get('description', result)}")
        except Exception as e:
            failed.append(f"Telegram: {e}")

    log.info(f"失败告警发送完成: {_send_alert_summary(failed)}")


def any_delivered(wechat_ok: int, tg_ok: bool | None) -> bool:
    """是否至少有一个推送渠道成功送达。

    用于决定「能否保存去重记录」：只有真正送达，才把这批链接标记已推送；
    否则全部失败时不保存，确保下次还能重试，不让简报静默丢失。

    · wechat_ok: Server酱成功送达的人数（0 = 全失败或未配置）
    · tg_ok:     Telegram 结果（True=成功 / False=失败 / None=未配置）
    """
    return wechat_ok > 0 or tg_ok is True
