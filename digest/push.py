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

import html
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

# Server酱 desp 内容字段官方上限 32KB（按 UTF-8 字节算）。中文每字 3 字节、
# emoji 4 字节，整包超限时网关会在上传途中重置 TCP 连接，表现为
# ConnectionResetError(104)。留约 1.7KB 余量给分段头与 markdown 转义。
SERVERCHAN_MAX_BYTES = 31000


def _utf8_len(s: str) -> int:
    """返回字符串的 UTF-8 字节长度（Server酱 按字节计长度）。"""
    return len(s.encode("utf-8"))


def _hard_split_by_bytes(text: str, max_bytes: int) -> list[str]:
    """逐字符累加切分，保证每段 UTF-8 字节数 ≤ max_bytes。

    逐「字符」而非逐「字节」累加，绝不会在一个多字节字符中间切断
    （否则会产生乱码 �）。用于单个段落本身就超限的兜底场景。
    """
    chunks: list[str] = []
    buf = ""
    buf_bytes = 0
    for ch in text:
        cb = _utf8_len(ch)
        if buf and buf_bytes + cb > max_bytes:
            chunks.append(buf)
            buf, buf_bytes = ch, cb
        else:
            buf += ch
            buf_bytes += cb
    if buf:
        chunks.append(buf)
    return chunks


def _split_by_bytes(content: str, max_bytes: int) -> list[str]:
    """把内容按 UTF-8 字节上限切分，优先在段落边界（空行）切。

    · 整体不超限 → 原样返回单段（与历史行为一致，不影响普通短简报）
    · 超限 → 按 \\n\\n 段落累加，单段超限时退化为 _hard_split_by_bytes
    返回至少 1 段。
    """
    if _utf8_len(content) <= max_bytes:
        return [content]

    chunks: list[str] = []
    current = ""
    for para in content.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if _utf8_len(candidate) <= max_bytes:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if _utf8_len(para) > max_bytes:
            chunks.extend(_hard_split_by_bytes(para, max_bytes))
        else:
            current = para
    if current:
        chunks.append(current)
    return chunks or [content]


def _serverchan_send_chunk(
    key: str, title: str, desp: str, label: str, failed: list[str]
) -> bool:
    """向 Server酱 发送单段内容，内置 3 次指数退避重试。

    返回是否成功送达。失败时把原因记入 failed 列表（不抛异常）。
    应对 GitHub Actions 海外 runner 连接国内 Server酱 偶发的
    Connection reset / timeout。
    """
    RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.JSONDecodeError,  # 网关返回 HTML 错误页 → 瞬时，可重试
    )
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info(f"正在推送到 {label} (Server酱) ... 第 {attempt}/{MAX_RETRIES} 次")
            resp = requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title, "desp": desp},
                timeout=30,
            )
            # 5xx 是网关/服务端瞬时故障，按可重试处理（而非当永久失败丢弃）
            status = getattr(resp, "status_code", 200)
            if status >= 500:
                raise requests.exceptions.ConnectionError(f"HTTP {status} 网关错误")
            result = resp.json()
            if result.get("code") == 0:
                log.info(f"✅ {label} 推送成功！")
                return True
            log.error(f"❌ {label} 推送失败: {result}")
            failed.append(f"{label}: {result}")
            return False  # 业务错误不重试
        except RETRYABLE as e:
            if attempt == MAX_RETRIES:
                log.error(f"❌ {label} 推送异常（已重试 {MAX_RETRIES} 次）: {e}")
                failed.append(f"{label}: {e}")
            else:
                wait = 5 * (2 ** (attempt - 1))
                log.warning(
                    f"⚠️ {label} 推送失败（{type(e).__name__}），{wait}s 后重试..."
                )
                time.sleep(wait)
        except Exception as e:
            log.error(f"❌ {label} 推送异常（非网络错误，不重试）: {e}")
            failed.append(f"{label}: {e}")
            return False
    return False


def push_to_wechat(content: str, sendkeys: list[str]) -> int:
    """通过 Server酱 把内容推送到微信（支持多个 SendKey，每人一个）。

    Server酱 desp 上限 32KB（按 UTF-8 字节）。超限时整包 POST 会被网关
    重置连接（ConnectionResetError）。这里按字节自动分段，每段 ≤32KB
    独立发送、各自 3 次重试——保证完整内容不被截断、不静默丢失。

    返回成功送达的人数（0 = 全部失败）。某收件人的任一分段失败即视为
    该人未送达。调用方据此判断是否真正送达。
    """
    if not sendkeys:
        raise RuntimeError("❌ 没有设置 SERVERCHAN_SENDKEY，请检查 .env 文件")

    today_str = datetime.now(TZ).strftime("%m/%d")
    chunks = _split_by_bytes(content, SERVERCHAN_MAX_BYTES)
    total = len(chunks)

    failed: list[str] = []
    success_count = 0
    valid_keys = [k.strip() for k in sendkeys if k.strip()]
    for i, key in enumerate(valid_keys):
        base_label = f"收件人{i+1}" if len(valid_keys) > 1 else "微信"

        recipient_ok = True
        for idx, chunk in enumerate(chunks, 1):
            label = base_label if total == 1 else f"{base_label}({idx}/{total})"
            title = f"📰 每日全球要闻 — {today_str}"
            if total > 1:
                title += f"（{idx}/{total}）"  # title 上限 32 字符，加标记仍很短
            if not _serverchan_send_chunk(key, title, chunk, label, failed):
                recipient_ok = False
                break  # 一段失败即放弃该人后续分段，避免浪费与误导
            if idx < total:
                time.sleep(0.5)  # 段间短暂间隔，避免 Server酱 限速

        if recipient_ok:
            success_count += 1

    if failed:
        log.warning(f"部分推送失败 ({len(failed)} 段): {'; '.join(failed)}")
    else:
        log.info(
            f"🎉 全部推送成功（共 {len(valid_keys)} 人 × {total} 段）"
        )
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
        requests.exceptions.JSONDecodeError,  # 非 JSON 响应（502 页等）→ 可重试
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
                status = getattr(resp, "status_code", 200)
                if status >= 500:
                    raise requests.exceptions.ConnectionError(f"HTTP {status} 网关错误")
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
            # parse_mode=HTML 下必须转义 < > &，否则 traceback 里的尖括号/&
            # 会让 Telegram 解析失败 → 告警发不出（恰恰在最该收到告警时静默）
            safe_stage = html.escape(stage)
            safe_err = html.escape(error_msg[:500])
            tg_text = (
                f"⚠️ <b>每日要闻推送失败</b>\n\n"
                f"<b>失败环节</b>: {safe_stage}\n"
                f"<b>时间</b>: {now_str}\n\n"
                f"<b>错误</b>:\n<pre>{safe_err}</pre>\n\n"
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
