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
from pathlib import Path

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
    save_reps_sidecar,
    load_sent_github_repos,
    save_sent_github_repos,
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
    strip_audit_block,
)
from digest.triage import triage_with_deepseek  # noqa: E402
from digest.critique import refine_digest  # noqa: E402
from digest.scout import scout_for_gaps  # noqa: E402
from digest.linkage import tag_progress  # noqa: E402
from digest.finalcheck import dedup_secondary, split_items, density_floor  # noqa: E402
from digest.backfill import backfill_reference_depth  # noqa: E402
from digest.bio import pick_bio_breakthrough  # noqa: E402
from digest.github import pick_github_trending  # noqa: E402
from digest.signals import pick_signals  # noqa: E402
from digest.topics import generate_topics  # noqa: E402
from digest.evidence import build_evidence_cards  # noqa: E402
from digest.quality import strip_internal_article_ids, validate_main_digest_evidence  # noqa: E402
from digest.storage import (  # noqa: E402
    save_evidence_sidecar,
    save_quality_report,
    mark_artifact_bundle_status,
    prune_quality_artifacts
)
from digest.hermes_export import HermesExportError, export_run  # noqa: E402
from digest.hermes_intelligence_bundle import build_bundle  # noqa: E402
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
#  渲染辅助函数（纯代码拼接，不经 AI，确保不被改写）
# ═══════════════════════════════════════════════════

def _prepend_selection_table(summary: str, articles: list[dict]) -> str:
    """在导语后、正文前插入 📊 今日选稿决策表。
    只有 triage 写回了 ai_score/ai_reason 的条目才会出现在表格里。"""
    scored = [a for a in articles if "ai_score" in a]
    if not scored:
        return summary

    rows = ["## 📊 今日选稿决策\n", "| # | 评分 | 标题 | 理由 |", "|---|------|------|------|"]
    for i, a in enumerate(scored, 1):
        title = a.get("title", "")[:40]
        score = a.get("ai_score", "")
        reason = a.get("ai_reason", "")
        rows.append(f"| {i} | {score} | {title} | {reason} |")
    table = "\n".join(rows) + "\n"

    # 尝试插在「今日导语」段落之后（找第一个 ## 标题之前）
    first_section = summary.find("\n## ")
    if first_section != -1:
        return summary[:first_section] + "\n\n" + table + summary[first_section:]
    return table + "\n" + summary


def _insert_gap_section(summary: str, gaps: list[dict]) -> str:
    """在简报末尾（编辑手记前）插入 💡 信息差侦察板块。gaps 为空则跳过。"""
    if not gaps:
        return summary

    lines = ["\n## 💡 信息差侦察 · 今日认知增量\n"]
    for i, g in enumerate(gaps, 1):
        lines.append(f"### {i}. {g.get('title', '')}")
        if g.get("why_valuable"):
            lines.append(f"**为何有价值**：{g['why_valuable']}")
        if g.get("why_underreported"):
            lines.append(f"**为何被低估**：{g['why_underreported']}")
        if g.get("url"):
            lines.append(f"🔗 {g['url']}")
        lines.append("")
    section = "\n".join(lines)

    # 插在「编辑手记」或「自我审计」之前，找不到就追加末尾
    for marker in ["『编辑手记", "```自我审计", "## 编辑手记"]:
        idx = summary.find(marker)
        if idx != -1:
            return summary[:idx] + section + "\n" + summary[idx:]
    return summary + section


def _insert_bio_section(summary: str, bio: dict | None) -> str:
    """在简报末尾（编辑手记前）插入 🧬 生物前沿板块（每期 1 条）。bio 为 None 则跳过。"""
    if not bio:
        return summary

    lines = ["\n## 🧬 生物前沿 · 今日一则\n", f"### {bio.get('title', '')}"]
    if bio.get("summary_zh"):
        lines.append(f"**一句话**：{bio['summary_zh']}")
    if bio.get("source"):
        src = bio["source"]
        url = bio.get("url", "")
        lines.append(f"> 📰 来源：{src}（{url}）" if url else f"> 📰 来源：{src}")
    lines.append("")
    section = "\n".join(lines)

    for marker in ["『编辑手记", "```自我审计", "## 编辑手记"]:
        idx = summary.find(marker)
        if idx != -1:
            return summary[:idx] + section + "\n" + summary[idx:]
    return summary + section


def _insert_github_section(summary: str, repos: list[dict] | None) -> str:
    """在简报末尾（编辑手记前）插入 🔥 GitHub 热榜板块。repos 为空/None 则跳过。"""
    if not repos:
        return summary

    lines = ["\n## 🔥 GitHub 热榜 · 今日 5 选\n"]
    for i, r in enumerate(repos, 1):
        # 星数格式化：≥1000 显示 12.3k
        stars = r.get("stargazers_count", 0)
        if stars >= 1000:
            stars_str = f"{stars / 1000:.1f}k"
        else:
            stars_str = str(stars)

        # 语言标签
        lang = r.get("language", "")
        lang_part = f" · {lang}" if lang else ""

        # 角标
        kind = r.get("kind", "rising")
        badge = "🚀新晋" if kind == "rising" else "🏛️老牌"

        lines.append(f"### {i}. {r['full_name']}  ⭐ {stars_str}{lang_part}  {badge}")
        lines.append(r.get("description_zh", ""))
        lines.append(f"🔗 {r.get('url', '')}")
        lines.append("")
    section = "\n".join(lines)

    for marker in ["『编辑手记", "```自我审计", "## 编辑手记"]:
        idx = summary.find(marker)
        if idx != -1:
            return summary[:idx] + section + "\n" + summary[idx:]
    return summary + section


def _insert_signals_section(summary: str, signals: list[dict] | None) -> str:
    """在简报末尾（编辑手记前）插入 📡 信号监测板块。signals 为空/None 则跳过。"""
    if not signals:
        return summary

    lines = ["\n## 📡 信号监测 · 今日工具/玩法\n"]
    for i, s in enumerate(signals, 1):
        lines.append(f"### {i}. {s.get('title', '')}")
        if s.get("summary_zh"):
            lines.append(f"**一句话**：{s['summary_zh']}")
        if s.get("url"):
            lines.append(f"🔗 {s['url']}")
        if s.get("source"):
            lines.append(f"> 📡 来源：{s['source']}")
        lines.append("")
    section = "\n".join(lines)

    for marker in ["『编辑手记", "```自我审计", "## 编辑手记"]:
        idx = summary.find(marker)
        if idx != -1:
            return summary[:idx] + section + "\n" + summary[idx:]
    return summary + section


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

        log.info("🧠 R1 决策精选（triage）...")
        articles = triage_with_deepseek(articles)
        log.info(f"triage 后保留 {len(articles)} 条")

        log.info("🔍 信息差侦察兵启动（scout）...")
        gaps = scout_for_gaps()
        log.info(f"侦察兵发现 {len(gaps)} 条低曝光内容")

        log.info("🧬 生物前沿单槽挑选（bio）...")
        bio = pick_bio_breakthrough()
        log.info("生物板块：" + ("已选中 1 条" if bio else "本期无"))

        log.info("🔥 GitHub 热榜挑选（github）...")
        repos = pick_github_trending()
        log.info("GitHub 板块：" + (f"已选 {len(repos)} 条" if repos else "本期无"))

        log.info("📡 信号监测（signals）...")
        signals = pick_signals()
        log.info("信号板块：" + (f"已选 {len(signals)} 条" if signals else "本期无"))

        log.info("📰 抓取精选新闻的正文全文（仅 triage 选中条目）...")
        attach_fulltexts(articles)

        # ── #3 参考源深度回填：高分 reference 条目抓不到正文时，搜同题全文源回填 ──
        log.info("📚 参考源深度回填（backfill）...")
        backfill_reference_depth(articles)

        # ── #1 跨期事件串联：给确定是「旧事进展」的条目打 progress_of 标记 ──
        log.info("📈 跨期事件串联（linkage）...")
        tag_progress(articles)

        # ── P2-A 证据提取：在生成提示词前，提取每条文章的结构化证据 ──
        log.info("📑 构建事实证据卡片 (evidence)...")
        build_evidence_cards(articles)

        log.info("🤖 调用 DeepSeek 生成中文简报...")
        summary = summarize_with_deepseek(articles)

        # ── 自评重写环：DeepSeek 给成稿打分，低于阈值带问题清单重写一次 ──
        # ── P2-E 质量校验：根据 evidence cards 发现无支撑数字并要求重写 ──
        log.info("📝 自评重写与事实幻觉校验 (critique & quality)...")
        evidence_cards = [a["evidence_card"] for a in articles if "evidence_card" in a]
        quality_report = validate_main_digest_evidence(summary, evidence_cards)
        summary = refine_digest(summary, quality_report=quality_report, evidence_cards=evidence_cards)

        # ── #4-A 信息密度地板（观察期：只记日志摸阈值，暂不改稿）──
        thin = density_floor(split_items(summary))
        if thin:
            log.info(f"密度地板：{len(thin)} 条点评偏薄 → {thin}")

        # ── #2 成稿近重复终检：以主新闻标题为基准，剔除 scout/bio 撞题条目 ──
        news_titles = [a["title"] for a in articles]
        gaps, bio = dedup_secondary(news_titles, gaps, bio)

        # ── 插入选稿决策表 ──
        summary = _prepend_selection_table(summary, articles)
        # ── 插入信息差板块 ──
        summary = _insert_gap_section(summary, gaps)
        # ── 插入生物前沿板块（每期 1 条）──
        summary = _insert_bio_section(summary, bio)
        # ── 插入 GitHub 热榜板块（每期 5 条）──
        summary = _insert_github_section(summary, repos)
        # ── 插入信号监测板块（每期 3 条）──
        summary = _insert_signals_section(summary, signals)
        # ── 追加自媒体选题 ──
        summary += generate_topics(articles, gaps)

        # ── 去除 AI 自我审计块（内部自检用，读者无需看到）──
        summary = strip_audit_block(summary)

        # ── 在最终成稿上重新生成质量报告用于持久化 ──
        final_quality_report = validate_main_digest_evidence(summary, evidence_cards)

        # article_id is an internal validation marker and must never reach readers.
        summary = strip_internal_article_ids(summary)

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

        # 保存 GitHub 热榜去重记录（独立于新闻）
        if repos:
            save_sent_github_repos([r["full_name"] for r in repos])

        # 存档最终生成的简报
        save_digest_markdown(summary)
        # 存档本期 reps 的 sidecar（英文原标题），供下期跨期串联英↔英比对（#1）
        save_reps_sidecar(articles)

        # ── P2-C/D 存储 evidence sidecar 与质量报告 ──
        from datetime import datetime
        now = datetime.now(TZ)
        session = "AM" if 4 <= now.hour < 16 else "PM"
        run_key = now.strftime(f"%Y-%m-%d-{session}")

        evidence_ok = save_evidence_sidecar(articles)
        quality_ok = save_quality_report(final_quality_report)
        mark_artifact_bundle_status(run_key, evidence_ok, quality_ok)
        if evidence_ok and quality_ok:
            try:
                export_path = export_run(Path.cwd(), run_key)
                log.info("Hermes AI 情报导出完成: %s", export_path)
            except HermesExportError as exc:
                log.warning("HERMES_EXPORT_SKIPPED: %s", exc)
        try:
            bundle_path = build_bundle(Path.cwd())
            log.info("Hermes AI 情报汇总完成: %s", bundle_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("HERMES_INTELLIGENCE_BUNDLE_SKIPPED: %s", exc)
        prune_quality_artifacts(now)

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
            "triage_with_deepseek": "R1决策精选",
            "scout_for_gaps": "信息差侦察",
            "pick_bio_breakthrough": "生物前沿挑选",
            "pick_github_trending": "GitHub热榜",
            "pick_signals": "信号监测",
            "attach_fulltexts": "正文抓取",
            "backfill_reference_depth": "参考源回填",
            "tag_progress": "跨期事件串联",
            "summarize_with_deepseek": "DeepSeek AI总结",
            "refine_digest": "自评重写环",
            "dedup_secondary": "成稿近重复终检",
            "generate_topics": "自媒体选题生成",
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
