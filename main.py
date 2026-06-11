"""每日全球要闻推送 —— 入口脚本。

本文件刻意保持轻薄：只做"加载 .env → 主流程编排 → 异常告警"三件事。
所有业务逻辑都拆到 digest/ 包下，按职责分模块（详见 digest/__init__.py）。

为什么入口仍叫 main.py 而非 digest/cli.py：
README、.github/workflows/daily-digest.yml、运行.bat 等下游都写了
`python main.py`。重构最忌讳一次改多件事，入口点保持不变。

兼容性 re-export：
test_core.py / diagnose.py 仍可写 `from main import score_importance`
等旧调用方式。模块级 import 同时让 monkeypatch.setattr("main.requests.post", ...)
继续生效（requests 是 sys.modules 单例，patch 全局可见）。

⚠️ 字符串变量的 monkeypatch（如 TELEGRAM_BOT_TOKEN）必须 patch
   "digest.push.TELEGRAM_BOT_TOKEN" 才能影响真实行为，patch "main.X"
   只改 main 上的副本。test_core.py 中相关两处已更新。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time  # 让 `main.time` 存在，支持 monkeypatch.setattr("main.time.sleep", ...)
import traceback

import requests  # 让 `main.requests` 存在，支持 monkeypatch.setattr("main.requests.*", ...)
from dotenv import load_dotenv

# ── 加载 .env（必须在 digest.* 模块顶层 os.getenv() 之前完成）──
load_dotenv()

# ═══════════════════════════════════════════════════
#  兼容性 re-export：保持旧 `from main import X` 可用
# ═══════════════════════════════════════════════════

from digest.config import (  # noqa: E402
    TZ,
    SENT_LOG_FILE,
    RSS_FEEDS,
    HIGH_SIGNAL_KEYWORDS,
    MEDIUM_SIGNAL_KEYWORDS,
    LOW_VALUE_KEYWORDS,
    PERSONAL_KEYWORDS,
    SOURCE_TRUST,
    CLICKBAIT_PATTERNS,
    TITLE_STOPWORDS,
    BATCH_SIZE,
    _HIGH_SIGNAL_RE,
    _MEDIUM_SIGNAL_RE,
    _LOW_VALUE_RE,
    _PERSONAL_RE,
)
from digest.storage import (  # noqa: E402
    load_sent_links,
    save_sent_links,
    should_skip_session,
    load_recent_digests,
    save_digest_markdown,
)
from digest.scoring import (  # noqa: E402
    score_importance,
    title_keywords,
    extract_proper_nouns,
    same_story,
    cluster_and_boost,
    enforce_category_balance,
)
from digest.fetch import (  # noqa: E402
    parse_published,
    clean_html,
    _fetch_feed_content,
    fetch_all_feeds,
    fetch_one_fulltext,
    attach_fulltexts,
)
from digest.factcheck import (  # noqa: E402
    extract_numerical_claims,
    build_factcheck_notes,
    sanity_check_output,
)
from digest.ai import (  # noqa: E402
    DEEPSEEK_API_KEY,
    _build_system_prompt,
    _articles_to_text,
    _call_deepseek_once,
    _log_sanity,
    summarize_with_deepseek,
)
from digest.push import (  # noqa: E402
    SERVERCHAN_SENDKEY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    push_to_wechat,
    push_to_telegram,
    send_failure_alert,
    any_delivered,
    _send_alert_summary,
)

# ── 日志（与原 main.py 一致：root logger, INFO 级别）──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="每日全球要闻推送")
    parser.add_argument("--force", action="store_true", help="强制执行，跳过去重检查")
    args = parser.parse_args()

    # 让控制台输出统一走 UTF-8，遇到无法显示的字符（如 emoji）就替换而非崩溃。
    # 主要是兼容 Windows 默认的 GBK 终端；Linux/GitHub Actions 本就是 UTF-8，无副作用。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # 某些环境下流不支持 reconfigure，忽略即可

    # 同日同时段去重：防止多个 cron 触发导致重复推送
    if not args.force and should_skip_session():
        log.info("🎉 本次运行已跳过（同日同时段已完成推送）")
        return

    # 加载跨天推送记录，用于去重
    sent_links = load_sent_links()

    try:
        log.info("=" * 50)
        log.info("📡 开始抓取 RSS 新闻...")
        articles = fetch_all_feeds(skip_links=sent_links)
        log.info(f"共抓到 {len(articles)} 条有效新闻（已跳过 {len(sent_links)} 条历史已推送）")

        if not articles:
            raise RuntimeError("没有抓到任何新闻——所有 RSS 源均无新内容或全部被过滤/去重跳过")

        log.info("📰 抓取候选新闻的正文全文...")
        attach_fulltexts(articles)

        log.info("🤖 调用 DeepSeek 生成中文简报...")
        summary = summarize_with_deepseek(articles)

        # ── 多渠道推送 ──────────────────────────────────
        # Server酱（国内 → 微信）：GitHub Actions 海外 runner 可能被墙
        wechat_ok = 0
        if SERVERCHAN_SENDKEY:
            sendkeys = [k.strip() for k in SERVERCHAN_SENDKEY.split(",") if k.strip()]
            log.info(f"📲 推送到微信 Server酱（共 {len(sendkeys)} 人）...")
            wechat_ok = push_to_wechat(summary, sendkeys)
        else:
            log.info("📲 Server酱 未配置，跳过微信推送")

        # Telegram：海外 runner 直连，不受 GFW 影响
        tg_ok = push_to_telegram(summary)

        # ── 送达判定 ──────────────────────────────────
        # 只有至少一个渠道成功送达，才保存去重记录；全部失败则抛错，
        # 触发失败告警且【不】保存——确保这批新闻下次还能重试，不被永久跳过。
        if not any_delivered(wechat_ok, tg_ok):
            raise RuntimeError(
                "所有推送渠道均失败（微信全部失败，Telegram 失败或未配置）——"
                "本次简报未送达，已跳过保存推送记录以便下次重试"
            )

        # 保存本次候选链接，跨天去重
        candidate_links = [a["link"] for a in articles if a.get("link")]
        save_sent_links(candidate_links)

        # 存档最终生成的简报
        save_digest_markdown(summary)

        log.info("=" * 50)
        log.info("🎉 全部完成！")

        # 打印摘要到日志（方便在 GitHub Actions 里查看）
        print("\n" + "=" * 50)
        print(summary)
        print("=" * 50)

    except Exception as e:
        # ── 失败告警：推送简短告警到所有可用通道 ──
        err_msg = f"{type(e).__name__}: {e}"
        log.error(f"💥 流水线失败: {err_msg}")
        log.error(traceback.format_exc())

        # 推断失败阶段
        stage_map = {
            "fetch_all_feeds": "RSS抓取",
            "attach_fulltexts": "正文抓取",
            "summarize_with_deepseek": "DeepSeek AI总结",
            "push_to_wechat": "微信推送",
            "push_to_telegram": "Telegram推送",
            "save_sent_links": "记录保存",
        }
        stage = "未知环节"
        tb = traceback.format_exc()
        for func, label in stage_map.items():
            if func in tb:
                stage = label
                break

        try:
            send_failure_alert(err_msg, stage)
        except Exception as alert_err:
            log.error(f"发送失败告警本身也失败了: {alert_err}")

        # 重新抛出，让 GitHub Actions 知道这次运行失败了
        raise


if __name__ == "__main__":
    main()
