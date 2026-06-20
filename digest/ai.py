"""AI 层：prompt 构建 + DeepSeek 流式调用 + 分批合并。

为什么分批：单批喂太多条 → prompt 大 → 服务端流式超时掐断。
分批策略：每批独立写"新闻条目"（不含导语/编辑手记），最后合并阶段
让 AI 只写导语+结语+审计块，新闻正文用代码拼接（避免 AI 漏抄）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime

import requests

from .config import TZ, BATCH_SIZE, _PERSONAL_RE
from .factcheck import build_factcheck_notes, sanity_check_output
from .storage import load_recent_digests

log = logging.getLogger(__name__)

# 模块加载时一次性读取——保持与原 main.py 顶部行为一致
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")


def _build_system_prompt(is_batch: bool = False) -> str:
    """构建系统提示词。is_batch=True 时省略「导语」和「编辑手记」——仅写新闻条目。"""
    base = (
        "你是一位资深的中文财经科技新闻主编，每天为一位高知读者撰写深度『晨报』。\n"
        "你的风格像《财新》《FT中文网》：冷静专业，敢下判断、点出影响与看点，绝不做无观点的复述。\n"
        "\n"
        "【读者画像——决定什么对他重要、影响该往哪落】\n"
        "· 硬核 PC 游戏 / MOD 玩家：关注 Steam 与 Valve 动向、游戏引擎（Unreal/Unity）、显卡（RTX/Radeon/DLSS）与硬件价格。\n"
        "· 正在学 AI 编程：关注大模型发布、开源模型、AI 编程工具（Claude Code/Cursor/Copilot）、API 价格与本地部署。\n"
        "· 身处中国：所有国际/科技/财经事件都要主动评估「对中国市场、出海企业、国内供应链、本地玩家/开发者」的具体影响。\n"
        "\n"
        "【深度思考要求（在正式输出前的内部推理中完成）】\n"
        "1. 交叉比对：核对各篇素材之间的数据是否冲突，冲突时取多数或最权威来源并注明。\n"
        "2. 过滤降噪：剔除洋八卦、标题党、纯软文，宁缺毋滥。\n"
        "3. 连续性判断：对照我附上的「近期已报道事件清单」，判断今天哪些是旧事件的新进展。\n"
        "4. 本土化与个人相关性：评估每条对「中国」和「上述读者画像」的具体意义。\n"
        "\n"
        "【怎么用素材——这条最重要】\n"
        "· 有【正文】的：通读后用自己的话写出来龙去脉，提炼关键事实、数据、人物、因果。\n"
        "· 只有【摘要】的：据实简写，明确不脑补正文里没有的细节。\n"
        "· 严禁编造：人名、数字、引语、时间、因果，凡素材没有的一律不写。宁可短，不可假。\n"
        "· 看到「各家标题」列出多家媒体对同一事件的不同标题时：若措辞/归因/立场明显不同，在【深层逻辑】里点出\"谁在怎么说\"。\n"
        "· 看到「近期已报道」清单中出现过的事件：以「📈 进展」开头，一句话交代\"此前→现在→变化意味着什么\"，不要当新事件从头讲。仅当确为同一事件时才这样做，不确定就当新事件。\n"
        "· 看到「🎯 个人雷达命中」标记的条目：【后市/影响】必须落到对上述读者画像的具体影响（显卡价格、工具链变化、可上手的新东西、出海/合规影响），不许写泛泛的\"利好行业\"。\n"
        "\n"
        "【每条新闻的标记与格式】\n"
        "标题行：**{重要性标记} {中文标题}**\n"
        "　重要性标记规则（可叠加）：🔥=被3+家媒体报道或极高重要性；⭐⭐⭐=必读 / ⭐⭐=值得看 / ⭐=速览；\n"
        "　　若命中个人雷达，标题再加 🎯；若是旧事件进展，标题再加 📈。\n"
        "正文三段（严格保留小标题，每条新闻的总字数严格控制在 200~300 字以内，务必精炼）：\n"
        "- **【核心事实】**：1-2 句概括\"发生了什么\"及核心数据（50字内）。\n"
        "- **【深层逻辑】**：一针见血指出事件背后的动机/商业逻辑/争议点；多家框架不同处点出立场差异（100字内）。\n"
        "- **【后市/影响】**：主编判断。结合本土化视角与读者画像，点明对国内市场/出海产业/宏观/该读者的潜在影响（100字内）。\n"
        "信息密度标签：📖 深度（有完整正文）或 📡 快讯（仅摘要/参考源）。\n"
        "末行来源（不可省略）：> 📰 来源：媒体名（原文链接）\n"
        "\n"
        "【硬性要求】\n"
        "· 全程中文，专有名词首次出现可附英文原名。\n"
        "· 三段式结构严格保持，点评要有信息增量和观点。\n"
        "· 每条「📰 来源」必须写明媒体名和原文链接，一条都不能省。\n"
        "· 仅单一来源的独家报道，在点评末尾注明「⚠️ 单一信源」。\n"
    )

    if is_batch:
        return (
            base
            + "\n【输出结构】用 Markdown 严格按以下三大板块分类输出新闻（若某板块无新闻请省略标题）：\n"
              "## 🌍 国际要闻\n"
              "## 💻 科技与 AI\n"
              "## 💰 财经市场\n"
              "\n"
              "【任务】我会给你一批候选新闻。请将它们按格式写成简报，并分类到上述三个板块下。绝对不要输出今日导语、编辑手记等其他内容！除非是纯软文或毫无信息量的凑数内容，否则尽量保留，不要过度删减。\n"
        )
    else:
        return (
            base
            + "\n【输出结构】用 Markdown：\n"
              "1. 顶部『今日导语』（3-4 句）概括今天全球主线与基调，末尾加一行市场情绪温度计：🟢 风险偏好 / 🟡 谨慎观望 / 🔴 避险为主（三选一）。\n"
              "2. 分三组（哪组没料就省略）：## 🌍 国际要闻 / ## 💻 科技与 AI / ## 💰 财经市场\n"
              "3. 每条严格按【核心事实】【深层逻辑】【后市/影响】三段写。\n"
              "4. 结尾『编辑手记 / 今日看点』（3-5 句）串联脉络，给出前瞻或提醒。\n"
              "5. 最末加『自我审计』代码块，逐条回答：\n"
              "   - 有无编造未在素材中出现的数字/人名/引语？（应为\"无\"）\n"
              "   - 标 📈 进展的条目，是否确与「近期已报道」清单中同一事件？\n"
              "   - 标 🎯 个人雷达的条目，影响是否落到了读者画像的具体层面？\n"
              "\n"
              "【任务】我会给你一批已初筛的候选新闻（已按重要性排序），请先在内部完成推理与事实审计，然后编成今天的深度晨报。除非是纯软文或毫无信息量的凑数内容，否则尽量保留，确保国际、科技、财经三大类均有涉及，不要过度删减。\n"
        )


def _articles_to_text(articles: list[dict]) -> str:
    """把文章列表转成发给 AI 的文本块。"""
    parts = []
    for i, art in enumerate(articles, 1):
        p = [f"{i}. [{art['category']}] {art['title']}"]

        cluster_size = art.get("cluster_size", 1)
        if cluster_size >= 2:
            p.append(f"   热度: 被 {cluster_size} 家媒体同时报道")

        cluster_titles = art.get("cluster_titles", [])
        if cluster_titles:
            p.append(f"   各家标题: {' / '.join(cluster_titles)}")

        # 个人雷达命中判断
        text_to_check = f"{art['title']} {art.get('summary', '')}".lower()
        hits = set(_PERSONAL_RE.findall(text_to_check))
        if hits:
            p.append(f"   🎯 个人雷达命中: {', '.join(hits)}")

        # 跨期事件串联（linkage.tag_progress 写入的 progress_of）：
        # 把「靠 LLM 肉眼找旧事件」降级成「代码喂确定的 此前→现在 对」，收窄幻觉面。
        po = art.get("progress_of")
        if po:
            p.append(
                f"   📈 此事曾于 {po['date']} 报道为「{po['prev_zh']}」，"
                f"请按\"进展\"写（此前→现在→变化意味着什么）"
            )

        fulltext = art.get("fulltext", "")
        if fulltext:
            p.append(f"   正文: {fulltext}")
            # 来源诚实（红线）：正文若来自跨源回填（backfill），必须如实标注，
            # 否则把 B 媒体的报道安到 A 媒体头上 = 张冠李戴 = 幻觉。
            backfill_source = art.get("backfill_source")
            if backfill_source:
                p.append(
                    f"   ⚠️ 正文据 {backfill_source} 同题报道；"
                    f"原报道来源 {art['source']}（{art.get('link', '')}）"
                )
        elif art["summary"]:
            p.append(f"   摘要(仅导语，正文未抓到): {art['summary']}")

        p.append(f"   来源: {art['source']}")
        if art["link"]:
            p.append(f"   原文链接: {art['link']}")
        parts.append("\n".join(p))
    return "\n\n".join(parts)


def _call_deepseek_once(system_prompt: str, user_prompt: str,
                        max_tokens: int = 8000,
                        model: str = "deepseek-v4-pro") -> dict:
    """单次调用 DeepSeek（流式 + 自动重试）。
    返回 {"choices": [{"message": {"content": ...}}], ...} 或抛异常。"""
    RETRYABLE = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": max_tokens,
                    # V4 思考模式开关（v4-flash/v4-pro 通用）。显式 enabled，把意图钉死，
                    # 不赖 API 默认。旧别名 deepseek-chat/reasoner 2026/07/24 废弃，此处迁移配套。
                    "thinking": {"type": "enabled"},
                },
                timeout=(60, 300),
                stream=True,
            )
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")

            # ── 非流式：temperature 等参数可能导致 DeepSeek 忽略 stream:true，
            #     直接返回 application/json。此时用 resp.json() 解析。
            if "application/json" in ct:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {
                    "choices": [{"message": {"content": content}}],
                    "usage": data.get("usage", {"total_tokens": "?"}),
                }

            # ── 流式：手动拼接 SSE 流式响应
            chunks: list[str] = []
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    delta = json.loads(data_str)
                    choice = delta.get("choices", [{}])[0]
                    chunk_text = choice.get("delta", {}).get("content", "")
                    if chunk_text:
                        chunks.append(chunk_text)
                except Exception:
                    continue
            content = "".join(chunks)
            return {
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": "?"},
            }
        except RETRYABLE as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 5 * (2 ** (attempt - 1))
            log.warning(
                f"DeepSeek 第 {attempt}/{MAX_RETRIES} 次失败（{type(e).__name__}），"
                f"{wait}s 后重试..."
            )
            time.sleep(wait)


def _log_sanity(content: str) -> None:
    """输出端轻量扫描：检测常见幻觉模式，在日志中提醒。"""
    warnings = sanity_check_output(content)
    if warnings:
        log.warning(f"⚠️ 幻觉风险提示（{len(warnings)} 项）:")
        for w in warnings:
            log.warning(f"  {w}")


def summarize_with_deepseek(articles: list[dict]) -> str:
    """把新闻列表发给 DeepSeek，让它用中文总结成每日简报。
    超过 BATCH_SIZE 条时自动拆批，每批独立写，最后拼成完整晨报。"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("❌ 没有设置 DEEPSEEK_API_KEY，请检查 .env 文件")

    n = len(articles)
    today_str = datetime.now(TZ).strftime("%Y年%m月%d日 %A")

    # ── 不分批：直接一次调用 ──
    if n <= BATCH_SIZE:
        log.info(f"发送 {n} 条候选到 DeepSeek（单批）...")
        system_prompt = _build_system_prompt(is_batch=False)
        articles_text = _articles_to_text(articles)

        recent_digests = load_recent_digests()

        factcheck_notes = build_factcheck_notes(articles)
        if factcheck_notes:
            articles_text = articles_text + "\n\n---\n\n" + factcheck_notes

        user_prompt = (
            f"今天是 {today_str}。以下是已初筛的候选新闻（共 {n} 条），"
            f"请按要求精选并编成今天的深度晨报。"
            f"每条新闻末尾务必附上「📰 来源：媒体名（原文链接）」。\n"
        )
        if recent_digests:
            user_prompt += f"\n{recent_digests}\n"
        user_prompt += f"\n{articles_text}"

        data = _call_deepseek_once(system_prompt, user_prompt)
        content = data["choices"][0]["message"]["content"]
        log.info(f"DeepSeek 返回 {len(content)} 字")
        _log_sanity(content)
        return content

    # ── 分批模式：拆成多批，每批独立写新闻条目，最后拼起来 ──
    batches = [articles[i:i + BATCH_SIZE] for i in range(0, n, BATCH_SIZE)]
    log.info(f"候选 {n} 条 → 分 {len(batches)} 批发送（每批 ≤{BATCH_SIZE} 条）")

    batch_outputs: list[str] = []
    for bi, batch in enumerate(batches, 1):
        log.info(f"  发送第 {bi}/{len(batches)} 批（{len(batch)} 条）...")
        system_prompt = _build_system_prompt(is_batch=True)
        articles_text = _articles_to_text(batch)

        user_prompt = (
            f"今天是 {today_str}。以下是今天新闻的第 {bi} 批（共 {len(batch)} 条）。\n"
            f"请仔细阅读并将其分类输出（必须且只能放入 🌍 国际要闻、💻 科技与 AI、💰 财经市场 这三个标题下）。"
            f"不要输出导语和编辑手记。每条末尾附「📰 来源：媒体名（原文链接）」。\n"
        )

        recent_digests = load_recent_digests()
        if recent_digests:
            user_prompt += f"\n{recent_digests}\n"
        user_prompt += f"\n{articles_text}"

        data = _call_deepseek_once(system_prompt, user_prompt, max_tokens=8000)
        text = data["choices"][0]["message"]["content"]
        log.info(f"  第 {bi} 批返回 {len(text)} 字")
        batch_outputs.append(text)

    # ── 拼合：用代码将各批次按板块归类 ──
    sections_data: dict[str, list[str]] = {"🌍 国际要闻": [], "💻 科技与 AI": [], "💰 财经市场": [], "未分类": []}
    for text in batch_outputs:
        current_sec = "未分类"
        for line in text.split('\n'):
            line_s = line.strip()
            if "🌍 国际要闻" in line_s and line_s.startswith("##"):
                current_sec = "🌍 国际要闻"
            elif "💻 科技与 AI" in line_s and line_s.startswith("##"):
                current_sec = "💻 科技与 AI"
            elif "💰 财经市场" in line_s and line_s.startswith("##"):
                current_sec = "💰 财经市场"
            else:
                sections_data[current_sec].append(line)

    grouped_parts = []
    for sec in ["🌍 国际要闻", "💻 科技与 AI", "💰 财经市场", "未分类"]:
        content = "\n".join(sections_data[sec]).strip()
        if content:
            if sec != "未分类":
                grouped_parts.append(f"## {sec}\n{content}")
            else:
                grouped_parts.append(content)

    all_news = "\n\n".join(grouped_parts)
    log.info(f"各批合计 {sum(len(o) for o in batch_outputs)} 字，已通过代码完成板块归类合并。准备补导语...")

    merge_prompt = (
        "你是一位资深中文新闻主编。以下是我已经通过程序排版好的今天的新闻简报正文。\n"
        "【任务】：请你仔细阅读这些新闻内容，然后专门为它写一段『今日导语』和一段『编辑手记 / 今日看点』，最后附上审计块。\n"
        "【特别警告】：绝对不要重写、复述或包含任何新闻条目的正文内容！新闻正文我会在程序里自己插入。\n"
        "\n"
        "阅读材料：\n\n"
        f"{all_news}\n\n"
        "请按以下精确格式输出（其中 {{NEWS}} 是占位符，你必须原样输出这几个英文字母，不要替换成新闻内容！）：\n\n"
        "『今日导语』\n"
        "（3-4 句概括今天全球主线，末尾加市场情绪温度计）\n\n"
        "{{NEWS}}\n\n"
        "『编辑手记 / 今日看点』\n"
        "（3-5 句串联脉络+前瞻）\n\n"
        "```自我审计\n"
        "（逐条回答审计问题）\n"
        "```\n"
    )

    # 事实核查笔记也在合并阶段注入
    factcheck_notes = build_factcheck_notes(articles)
    if factcheck_notes:
        merge_prompt = merge_prompt + "\n\n---\n\n⚠️ 事前核查提醒，供参考撰写编辑手记：\n" + factcheck_notes

    log.info("  发送合并请求...")
    data = _call_deepseek_once(
        "你是资深新闻主编，只需输出导语和结语。不要输出新闻正文，必须用 {{NEWS}} 占位符原样替代！",
        merge_prompt,
        max_tokens=8000,
    )
    final_output = data["choices"][0]["message"]["content"]

    # 用 Python 替换占位符，拼接最终内容
    if "{{NEWS}}" in final_output:
        final = final_output.replace("{{NEWS}}", all_news)
    else:
        # 如果 AI 漏写了占位符，做 fallback 追加在中间
        log.warning("AI 合并输出漏写了占位符，采用后备方案拼接。")
        parts = final_output.split("『编辑手记", 1)
        if len(parts) == 2:
            final = parts[0] + "\n\n" + all_news + "\n\n『编辑手记" + parts[1]
        else:
            final = final_output + "\n\n" + all_news

    log.info(f"合并后最终成文 {len(final)} 字")
    _log_sanity(final)
    return final


def strip_audit_block(text: str) -> str:
    """删除 AI 自我审计代码块，该块仅供 AI 内部自检，不应出现在推送内容里。"""
    return re.sub(r"```自我审计[\s\S]*?```", "", text).strip()
