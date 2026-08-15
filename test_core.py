"""核心业务逻辑单元测试 —— 打分 & 聚类去重

运行: python -m pytest test_core.py -v
"""

import sys
import os
from datetime import datetime, timezone, timedelta

import requests

# 确保能 import main（测试脚本不在项目根目录也能跑）
sys.path.insert(0, os.path.dirname(__file__))

from main import (
    score_importance,
    title_keywords,
    extract_proper_nouns,
    same_story,
    cluster_and_boost,
    sanity_check_output,
    any_delivered,
    _fetch_feed_content,
    push_to_wechat,
    push_to_telegram,
    _insert_github_section,
)


# ═══════════════════════════════════════════════════
#  score_importance 测试
# ═══════════════════════════════════════════════════

class TestScoreImportance:
    """验证打分逻辑：关键词命中、新鲜度、来源可信度、标题党惩罚"""

    def test_high_signal_keyword_adds_3(self):
        """命中高权重关键词（如 nuclear）应 +3"""
        score = score_importance("Nuclear talks reach breakthrough", "", None)
        assert score >= 3, f"期望 ≥3，实际 {score}"

    def test_multiple_keywords_stack(self):
        """多个关键词命中应叠加"""
        score = score_importance(
            "AI chip ban causes market crash after fed rate cut",
            "sanctions and tariffs rising",
            None,
        )
        # chip ban(+3) + rate cut(+3) + tariff(+3) + sanctions(+3)
        assert score >= 12, f"期望≥12（4个高权重词×3），实际 {score}"

    def test_low_value_keyword_penalizes(self):
        """命中低价值关键词应扣分"""
        score = score_importance("Celebrity chef shares top 10 recipes", "", None)
        # celebrity(-2) + top 10(-2) + recipe(-2) = -6
        assert score < 0, f"期望负分，实际 {score}"

    def test_personal_keywords_adds_4(self):
        """命中个人雷达关键词应 +4"""
        score = score_importance("New steam deck announced with rtx gpu", "", None)
        # steam (+4) + rtx (+4) = 8
        assert score >= 8, f"期望 ≥8，实际 {score}"

    def test_freshness_bonus(self):
        """6 小时内新闻应拿新鲜度加分"""
        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        score_recent = score_importance("Some news", "", recent)
        score_old = score_importance("Some news", "", old)
        assert score_recent > score_old, f"新 {score_recent} 应 > 旧 {score_old}"

    def test_source_trust_adds_points(self):
        """高信任度来源应加分"""
        score = score_importance("Some news", "", None, source="Reuters")
        assert score >= 2, f"Reuters 应+2，实际 {score}"

    def test_clickbait_title_penalized(self):
        """标题党句式应被 -2"""
        score_with = score_importance("This is why markets crashed yesterday?", "", None)
        score_without = score_importance("Markets crash on tariff fears", "", None)
        assert score_with < score_without, (
            f"标题党 {score_with} 应 < 正常 {score_without}"
        )

    def test_empty_input_does_not_crash(self):
        """空标题/摘要不应抛异常"""
        score = score_importance("", "", None, source="")
        assert isinstance(score, (int, float)), f"应返回数字，实际 {type(score)}"

    def test_hard_number_signal_adds_point(self):
        """标题含具体金额/量级（实锤数字）应比无数字版多 +1。"""
        with_num = score_importance("Company reports $2 billion loss", "", None)
        without = score_importance("Company reports big loss", "", None)
        assert with_num > without, (
            f"带实锤数字 {with_num} 应 > 无数字 {without}"
        )

    def test_percent_counts_as_hard_number(self):
        """百分比也算实锤数字（40% → +1）。"""
        with_pct = score_importance("Exports drop 40% in quarter", "", None)
        without = score_importance("Exports drop sharply in quarter", "", None)
        assert with_pct > without, f"带百分比 {with_pct} 应 > 无 {without}"

    def test_bare_year_not_counted_as_hard_number(self):
        """裸年份（2026）不是数据，不该触发实锤数字加分——防误命中。"""
        # 无任何信号词、无符号/量级词，仅含一个年份
        score = score_importance("Festival returns in 2026", "", None)
        assert score <= 0, f"裸年份不应加分，期望 ≤0，实际 {score}"

    def test_entity_density_adds_point(self):
        """标题含 ≥2 个专有名词（具体事件）应 +1；单个专有名词不加。"""
        dense = score_importance("Macron meets Scholz in Berlin", "", None)
        sparse = score_importance("Macron travels overseas", "", None)
        assert dense > sparse, (
            f"多专有名词 {dense} 应 > 单专有名词 {sparse}"
        )


# ═══════════════════════════════════════════════════
#  关键词匹配边界测试 —— 防止子串误匹配回归
# ═══════════════════════════════════════════════════
#
# 历史 bug：旧实现用 `kw in text` 做子串匹配，导致：
#   · "ai"  命中  said / again / rain / paid
#   · "war" 命中  warning / award / software（+3 分！）
#   · "ev"  命中  every / even / level / never
#   · "ban" 命中  bank / urban
#   · "oil" 命中  boil / turmoil
# 这一组测试钉死边界匹配行为，防止以后回归到子串匹配。

class TestKeywordBoundaryMatching:
    """关键词应做"整词"匹配，不能被其他词的子串误命中。"""

    def test_short_keyword_ai_does_not_match_said(self) -> None:
        """`ai` 不应被 'said' / 'again' / 'rain' 误命中。"""
        # 标题只含会让旧实现误匹配的词，不含真正的 AI 信号
        score = score_importance(
            "President said it again on the campaign rain trail",
            "",
            None,
        )
        # 没有任何真信号词 → 应为 0；旧 bug 会因 'ai' 子串命中得到 +3 以上
        assert score <= 0, (
            f"'said/again/rain' 不应被 'ai' 子串误匹配，期望 ≤0，实际 {score}"
        )

    def test_short_keyword_war_does_not_match_warning(self) -> None:
        """`war` (+3) 不应被 'warning' / 'award' / 'software' 误命中。"""
        score = score_importance(
            "Weather warning issued for award ceremony software glitch",
            "",
            None,
        )
        assert score <= 0, (
            f"'warning/award/software' 不应被 'war' 子串误匹配，期望 ≤0，实际 {score}"
        )

    def test_short_keyword_ev_does_not_match_every(self) -> None:
        """`ev` (+1) 不应被 'every' / 'even' / 'level' / 'never' 误命中。"""
        score = score_importance(
            "Every level of government has never seen even one such case",
            "",
            None,
        )
        assert score <= 0, (
            f"'every/even/level/never' 不应被 'ev' 子串误匹配，期望 ≤0，实际 {score}"
        )

    def test_short_keyword_ban_does_not_match_bank(self) -> None:
        """`ban` (+1) 不应被 'bank' / 'urban' 误命中。"""
        score = score_importance("Urban bank opens new branch", "", None)
        assert score <= 0, (
            f"'urban/bank' 不应被 'ban' 子串误匹配，期望 ≤0，实际 {score}"
        )

    def test_short_keyword_oil_does_not_match_turmoil(self) -> None:
        """`oil` (+1) 不应被 'boil' / 'turmoil' 误命中。"""
        score = score_importance("Region in turmoil as pot starts to boil", "", None)
        assert score <= 0, (
            f"'turmoil/boil' 不应被 'oil' 子串误匹配，期望 ≤0，实际 {score}"
        )

    def test_real_keyword_still_matches_when_standalone(self) -> None:
        """整词出现的关键词必须仍能正常命中（不要修过头）。"""
        score = score_importance("AI breakthrough announced today", "", None)
        # ai(+1) + breakthrough(+3) ≥ 4
        assert score >= 4, (
            f"独立单词 'AI' 和 'breakthrough' 应该被识别，期望 ≥4，实际 {score}"
        )

    def test_multiword_keyword_still_matches(self) -> None:
        """带空格的词组（如 'rate cut'）必须仍能命中。"""
        score = score_importance("Fed announces rate cut after meeting", "", None)
        # rate cut(+3) + fed(+3) ≥ 6
        assert score >= 6, (
            f"'rate cut' / 'fed' 词组应被识别，期望 ≥6，实际 {score}"
        )

    def test_keyword_at_string_boundary_matches(self) -> None:
        """关键词出现在标题开头或末尾也应命中（边界正则不能漏掉句首句尾）。"""
        # 句首
        s1 = score_importance("War breaks out", "", None)
        assert s1 >= 3, f"句首 'War' 应该命中 +3，实际 {s1}"
        # 句尾
        s2 = score_importance("Country declares war", "", None)
        assert s2 >= 3, f"句尾 'war' 应该命中 +3，实际 {s2}"

    def test_keyword_with_punctuation_still_matches(self) -> None:
        """关键词紧邻标点（逗号/句号/引号）也应命中。"""
        score = score_importance("Nuclear, Iran says, is off the table.", "", None)
        assert score >= 3, f"'Nuclear,' 应该命中 +3，实际 {score}"


# ═══════════════════════════════════════════════════
#  title_keywords 和 extract_proper_nouns 测试
# ═══════════════════════════════════════════════════

class TestTokenization:
    """验证标题关键词提取和专有名词识别"""

    def test_removes_stopwords(self):
        """虚词（the, a, of, says 等）应被滤掉"""
        words = title_keywords("The president says crisis is over")
        assert "the" not in words
        assert "says" not in words
        assert "president" in words
        assert "crisis" in words

    def test_extracts_proper_nouns(self):
        """首字母大写的词和全大写缩写应被识别为专有名词"""
        pn = extract_proper_nouns("Ukraine and Russia agree to ceasefire deal")
        assert "ukraine" in pn
        assert "russia" in pn

    def test_extracts_acronyms(self):
        """全大写缩写应被提取（注意 WHO 会被 generic 过滤，用 NASA/FBI 不冲突）"""
        pn = extract_proper_nouns("NASA announces new mission with FBI support")
        assert "nasa" in pn
        assert "fbi" in pn

    def test_extracts_chinese(self):
        """中文词应被提取"""
        pn = extract_proper_nouns("北京召开两会讨论经济政策")
        assert any("北京" in p or "两会" in p for p in pn)


# ═══════════════════════════════════════════════════
#  same_story 测试 — 同题聚类核心逻辑
# ═══════════════════════════════════════════════════

class TestSameStory:
    """验证两条标题是否在讲同一件事的判断逻辑"""

    def test_shared_proper_noun_same_story(self):
        """共享专有名词 + ≥2 普通关键词 → 同事件"""
        w_a = title_keywords("Ukraine launches counteroffensive in Kharkiv region")
        w_b = title_keywords("Russia says Ukraine counteroffensive failed")
        pn_a = extract_proper_nouns("Ukraine launches counteroffensive in Kharkiv region")
        pn_b = extract_proper_nouns("Russia says Ukraine counteroffensive failed")
        assert same_story(w_a, w_b, pn_a, pn_b), "共享 Ukraine + counteroffensive 应判同事件"

    def test_different_proper_noun_not_same_story(self):
        """共享普通词但专有名词不共享 → 不同事件"""
        # A: {"president", "meets", "european", "leaders", "today"} (5), PN: {"president","european"}
        # B: {"minister", "visits", "asian", "officials", "today"} (5), PN: {"minister","asian"}
        # 共享词仅 {"today"} = 1，专名不共享 → 不判同事件
        w_a = title_keywords("President meets European leaders today")
        w_b = title_keywords("Minister visits Asian officials today")
        pn_a = extract_proper_nouns("President meets European leaders today")
        pn_b = extract_proper_nouns("Minister visits Asian officials today")
        result = same_story(w_a, w_b, pn_a, pn_b)
        assert not result, (
            f"专有名不同+仅1个共享词，不应判同事件。"
            f"共享词: {w_a & w_b}, 共享专名: {pn_a & pn_b}"
        )

    def test_no_proper_nouns_needs_more_shared(self):
        """双方都没有专有名词 → 阈值更高（≥4 或占比≥50%）"""
        w_a = title_keywords("New report shows climate change impact growing fast")
        w_b = title_keywords("Climate report warns of growing economic impact worldwide")
        # 共享: report, climate, growing, impact → 4 个，刚好过阈值
        result = same_story(w_a, w_b)
        assert result, (
            f"共享 ≥4 个关键词应判同事件。共享词: {w_a & w_b}"
        )

    def test_short_title_ratio_rule(self):
        """短标题 A 无专名，长标题 B 有专名 → 走 fallback 分支，共享≥3 即同事件"""
        # A: 3 关键词，无专名；B: 6 关键词，有专名 "Japan"
        # 共享: major, earthquake, today = 3 → fallback len(shared)≥3 → True
        w_a = title_keywords("major earthquake today")
        w_b = title_keywords("Japan major earthquake kills hundreds today")
        result = same_story(w_a, w_b)
        assert result, (
            f"共享 3 关键词应判同事件。共享词: {w_a & w_b}"
        )

    def test_completely_different_topics(self):
        """完全不搭边的标题 → 不同事件"""
        w_a = title_keywords("Scientists discover new species of deep sea fish")
        w_b = title_keywords("Stock market hits record high on tech earnings")
        result = same_story(w_a, w_b)
        assert not result, f"不相关标题不应判同事件。共享词: {w_a & w_b}"


# ═══════════════════════════════════════════════════
#  cluster_and_boost 集成测试
# ═══════════════════════════════════════════════════

class TestClusterAndBoost:
    """验证聚类去重 + 多源加分逻辑"""

    def test_same_event_gets_clustered(self):
        """同一事件多条报道应聚为一簇"""
        articles = [
            {"title": "Ukraine counteroffensive begins in Kharkiv region", "summary": "",
             "score": 10, "source": "BBC World", "category": "国际", "reference": False, "link": "http://a.com"},
            {"title": "Ukraine counteroffensive continues as forces advance", "summary": "",
             "score": 8, "source": "DW", "category": "国际", "reference": False, "link": "http://b.com"},
            {"title": "Stock market hits all-time high", "summary": "",
             "score": 7, "source": "CNBC", "category": "财经", "reference": False, "link": "http://c.com"},
        ]
        result = cluster_and_boost(articles)
        # 前两条共享 Ukraine+counteroffensive+专名 → 合并；第三条独立 → 2 簇
        assert len(result) == 2, f"期望 2 簇，实际 {len(result)}"

    def test_cluster_size_bonus_added(self):
        """被多家报道的事件应获得 cluster_size 加分"""
        articles = [
            {"title": "Federal Reserve raises interest rates today", "summary": "",
             "score": 10, "source": "FT", "category": "财经", "reference": False, "link": "http://a.com"},
            {"title": "Federal Reserve hikes interest rates again", "summary": "",
             "score": 9, "source": "CNBC", "category": "财经", "reference": False, "link": "http://b.com"},
            {"title": "Federal Reserve rate decision impacts global markets", "summary": "",
             "score": 8, "source": "BBC World", "category": "国际", "reference": False, "link": "http://c.com"},
        ]
        result = cluster_and_boost(articles)
        assert len(result) == 1, "三条应聚合为 1 簇"
        rep = result[0]
        assert rep["score"] >= 10, f"代表分应≥原始最高分，实际 {rep['score']}"
        assert rep["cluster_size"] == 3, f"簇大小应为 3，实际 {rep['cluster_size']}"

    def test_reference_source_not_preferred_as_rep(self):
        """参考源不应被选为代表作"""
        articles = [
            {"title": "Major earthquake hits Tokyo region today", "summary": "",
             "score": 12, "source": "Reuters", "category": "国际",
             "reference": True, "link": "http://ref.com"},
            {"title": "Massive earthquake strikes Tokyo area morning", "summary": "",
             "score": 10, "source": "BBC World", "category": "国际",
             "reference": False, "link": "http://bbc.com"},
        ]
        result = cluster_and_boost(articles)
        # 共享 Tokyo+earthquake+专名"tokyo" → 聚类；应选 BBC（非参考源）为代表
        assert result[0]["source"] == "BBC World", (
            f"非参考源应优先当选。实际代表: {result[0]['source']}"
        )

    def test_cluster_titles_added(self):
        """代表作应该记录其他同簇文章的标题"""
        articles = [
            {"title": "Ukraine counteroffensive begins", "summary": "",
             "score": 10, "source": "BBC World", "category": "国际", "reference": False, "link": "http://a.com"},
            {"title": "Ukraine counteroffensive continues", "summary": "",
             "score": 8, "source": "DW", "category": "国际", "reference": False, "link": "http://b.com"},
        ]
        result = cluster_and_boost(articles)
        assert len(result) == 1
        rep = result[0]
        assert "cluster_titles" in rep
        assert len(rep["cluster_titles"]) == 1
        assert "DW: Ukraine counteroffensive continues" in rep["cluster_titles"][0]

    def test_articles_sorted_by_final_score(self):
        """结果应按调整后分数降序"""
        articles = [
            {"title": "Minor local news item", "summary": "",
             "score": 1, "source": "Local News", "category": "国际", "reference": False, "link": "http://a.com"},
            {"title": "Global trade deal signed by 50 nations", "summary": "",
             "score": 15, "source": "FT", "category": "国际", "reference": False, "link": "http://b.com"},
            {"title": "Tech startup raises $100M", "summary": "",
             "score": 6, "source": "TechCrunch", "category": "科技", "reference": False, "link": "http://c.com"},
        ]
        result = cluster_and_boost(articles)
        scores = [r["score"] for r in result]
        assert scores == sorted(scores, reverse=True), f"应降序排列，实际 {scores}"


# ═══════════════════════════════════════════════════
#  sanity_check_output 测试
# ═══════════════════════════════════════════════════

class TestSanityCheck:
    """验证 AI 输出端的幻觉检测"""

    def test_future_date_detected(self):
        """检测到未来日期应告警"""
        warnings = sanity_check_output("会议定于 2026年12月15日举行")
        assert len(warnings) >= 1, f"未来日期应触发告警，实际 {warnings}"

    def test_missing_self_audit_warned(self):
        """缺少自我审计段应告警"""
        warnings = sanity_check_output("今天新闻：美联储加息。")
        assert any("自我审计" in w for w in warnings), (
            f"缺审计段应告警。告警: {warnings}"
        )

    def test_progress_ratio_warned(self):
        """进展条目超过 50% 应告警"""
        warnings = sanity_check_output(
            "📈 进展：事情发展了\n"
            "📰 来源：A\n"
        )
        assert any("进展条目占比" in w for w in warnings), f"超过 50% 进展应告警，实际: {warnings}"

    def test_normal_content_passes(self):
        """正常内容不应有过多告警"""
        warnings = sanity_check_output(
            "美联储宣布维持利率不变。"
            "📰 来源：FT\n"
            "📰 来源：BBC World\n"
            "📰 来源：Reuters\n"
            "📰 来源：CNBC\n"
            "📰 来源：DW\n"
            "📰 来源：Nikkei Asia\n"
            "📰 来源：AP\n"
            "📰 来源：Al Jazeera\n"
            "📰 来源：The Guardian\n"
            "自我审计：1.通过 2.通过 3.通过\n"
        )
        # 只保留"高精度数字"告警（如果有的话），其他不应触发
        severe = [w for w in warnings if "审计" in w or "未来" in w]
        assert len(severe) == 0, f"正常内容不应有严重告警: {severe}"


# ═══════════════════════════════════════════════════
#  Bug 1：推送送达判定 —— 全渠道失败时不应标记已推送
# ═══════════════════════════════════════════════════
#
# 历史 bug：push_to_wechat 失败只 log.warning 不抛错，push_to_telegram
# 未配置时直接 return。结果"微信全挂 + 没配 TG"时主流程照常走完、
# save_sent_links 把链接存档 → 这批新闻被永久跳过、且无失败告警 →
# 简报静默丢失。修复：用 any_delivered() 判定，全失败则 raise、不保存。

class TestDeliveryDecision:
    """any_delivered(wechat_ok, tg_ok)：是否至少一个渠道成功送达。"""

    def test_all_channels_failed_is_not_delivered(self) -> None:
        """微信全失败(0) + TG 未配置(None) → 未送达（核心 bug 场景）。"""
        assert any_delivered(0, None) is False
        # 微信全失败(0) + TG 也失败(False) → 未送达
        assert any_delivered(0, False) is False

    def test_wechat_success_is_delivered(self) -> None:
        """只要微信有人收到(>0) → 已送达，可保存记录。"""
        assert any_delivered(2, None) is True
        assert any_delivered(1, False) is True

    def test_telegram_success_is_delivered(self) -> None:
        """微信全挂但 TG 成功(True) → 已送达。"""
        assert any_delivered(0, True) is True


class TestWechatLongContentSplitting:
    """微信内容超过 Server酱 32KB 上限时应自动分段发多条。

    历史 bug：push_to_wechat 把整包内容一次性 POST，无任何长度处理。
    「保证不截断」更新后正文涨到 ~33KB（中文 UTF-8 每字 3 字节），
    超过 Server酱 desp 32KB 上限 → 网关在上传途中重置 TCP 连接，
    表现为 ConnectionResetError(104, 'Connection reset by peer')，重试 3 次
    全失败。Telegram 因有分段逻辑（按 3500 字符切）不受影响。
    修复：微信同样按 UTF-8 字节上限分段，每段独立发送。
    """

    def test_long_content_split_into_multiple_posts(self, monkeypatch) -> None:
        """超过 32KB 的内容应拆成多条 POST，每条 desp 都在字节上限内。"""
        captured_desps: list[str] = []

        class FakeResp:
            def json(self):
                return {"code": 0}

        def fake_post(url, **kwargs):
            captured_desps.append(kwargs["data"]["desp"])
            return FakeResp()

        monkeypatch.setattr("main.requests.post", fake_post)
        monkeypatch.setattr("main.time.sleep", lambda *a, **k: None)

        # 每段 11 个中文字 ×3 字节 ×200 ≈ 6.6KB，8 段 ≈ 52KB，远超 32KB
        para = "这是一段测试新闻内容。" * 200
        long_content = "\n\n".join([para] * 8)
        assert len(long_content.encode("utf-8")) > 32 * 1024  # 前提：确实超限

        n = push_to_wechat(long_content, ["onlykey"])

        assert n == 1, "单个 key 全部段落发成功 → 计 1 人"
        assert len(captured_desps) >= 2, (
            f"超长内容应拆成多条，实际只发了 {len(captured_desps)} 条"
        )
        for d in captured_desps:
            assert len(d.encode("utf-8")) <= 32 * 1024, (
                f"每条 desp 必须 ≤32KB，实际 {len(d.encode('utf-8'))} 字节"
            )

    def test_short_content_still_single_post(self, monkeypatch) -> None:
        """普通短内容仍只发 1 条（不回归，保持原行为）。"""
        posts: list[dict] = []

        class FakeResp:
            def json(self):
                return {"code": 0}

        def fake_post(url, **kwargs):
            posts.append(kwargs)
            return FakeResp()

        monkeypatch.setattr("main.requests.post", fake_post)
        monkeypatch.setattr("main.time.sleep", lambda *a, **k: None)
        push_to_wechat("今天的简短新闻", ["k"])
        assert len(posts) == 1, f"短内容应只发 1 条，实际 {len(posts)} 条"

    def test_one_chunk_fails_recipient_not_counted(self, monkeypatch) -> None:
        """多段中有一段最终失败 → 该收件人不计成功（返回 0）。"""
        calls = {"n": 0}

        class FakeResp:
            def __init__(self, code):
                self._code = code

            def json(self):
                return {"code": self._code}

        def fake_post(url, **kwargs):
            calls["n"] += 1
            # 第 2 条业务失败（code 500），其余成功
            return FakeResp(0 if calls["n"] != 2 else 500)

        monkeypatch.setattr("main.requests.post", fake_post)
        monkeypatch.setattr("main.time.sleep", lambda *a, **k: None)

        para = "测试内容内容内容内容。" * 200
        long_content = "\n\n".join([para] * 8)
        n = push_to_wechat(long_content, ["k"])
        assert n == 0, "有一段失败，该收件人不应算成功"


class TestPushReturnValues:
    """push_to_wechat / push_to_telegram 的返回值契约（mock 网络）。"""

    def test_wechat_all_fail_returns_zero(self, monkeypatch) -> None:
        """所有 SendKey 都收到业务错误码 → 返回成功数 0（而非静默 None）。"""
        class FakeResp:
            def json(self):
                return {"code": 500, "message": "boom"}

        monkeypatch.setattr("main.requests.post", lambda *a, **k: FakeResp())
        monkeypatch.setattr("main.time.sleep", lambda *a, **k: None)
        n = push_to_wechat("body", ["key1", "key2"])
        assert n == 0, f"全失败应返回 0，实际 {n}"

    def test_wechat_partial_success_returns_count(self, monkeypatch) -> None:
        """一个成功一个失败 → 返回成功数 1。"""
        calls = {"n": 0}

        class FakeResp:
            def __init__(self, code):
                self._code = code

            def json(self):
                return {"code": self._code}

        def fake_post(url, **kwargs):
            calls["n"] += 1
            # 第一个 key 成功(code 0)，第二个失败(code 500)
            return FakeResp(0 if calls["n"] == 1 else 500)

        monkeypatch.setattr("main.requests.post", fake_post)
        monkeypatch.setattr("main.time.sleep", lambda *a, **k: None)
        n = push_to_wechat("body", ["good", "bad"])
        assert n == 1, f"应返回成功数 1，实际 {n}"

    def test_telegram_unconfigured_returns_none(self, monkeypatch) -> None:
        """未配置 TG → 返回 None（表示'跳过'，不是'失败'）。"""
        # push_to_telegram 读取 digest.push 的模块级变量——必须 patch 那里。
        # patch "main.TELEGRAM_*" 只改 main 上的字符串副本，不影响真实行为。
        monkeypatch.setattr("digest.push.TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr("digest.push.TELEGRAM_CHAT_ID", "")
        assert push_to_telegram("hi") is None


# ═══════════════════════════════════════════════════
#  Bug 2：抓 RSS 源必须带超时 + 异常安全
# ═══════════════════════════════════════════════════
#
# 历史 bug：feedparser.parse(url) 内部用 urllib 下载，无超时参数。
# 任一源服务器挂起 → 整个 job 卡死（GitHub Actions 默认 6h 才杀）。
# 修复：改用 requests.get 带超时下载原始字节，再 feedparser.parse(bytes)。

class TestFetchFeedContent:
    """_fetch_feed_content：带超时下载，任何失败返回 None（绝不抛错）。"""

    def test_timeout_returns_none(self, monkeypatch) -> None:
        """请求超时 → 返回 None，不向上抛异常（不能拖死整个 job）。"""
        def fake_get(*a, **k):
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr("main.requests.get", fake_get)
        result = _fetch_feed_content({"name": "X", "url": "http://x"})
        assert result is None

    def test_passes_timeout_argument(self, monkeypatch) -> None:
        """必须给 requests.get 传 timeout 参数——这是本次修复的核心。"""
        captured = {}

        class FakeResp:
            content = b"<rss></rss>"

            def raise_for_status(self):
                pass

        def fake_get(url, **kwargs):
            captured.update(kwargs)
            return FakeResp()

        monkeypatch.setattr("main.requests.get", fake_get)
        result = _fetch_feed_content({"name": "X", "url": "http://x"})
        assert result == b"<rss></rss>"
        assert "timeout" in captured, "requests.get 必须带 timeout 参数"
        assert captured["timeout"], "timeout 不能为 0 或 None"

    def test_http_error_returns_none(self, monkeypatch) -> None:
        """HTTP 4xx/5xx（raise_for_status 抛错）→ 返回 None。"""
        class FakeResp:
            content = b""

            def raise_for_status(self):
                raise requests.exceptions.HTTPError("404")

        monkeypatch.setattr("main.requests.get", lambda *a, **k: FakeResp())
        assert _fetch_feed_content({"name": "X", "url": "http://x"}) is None

    def test_empty_url_returns_none(self) -> None:
        """没有 url 的源 → 直接返回 None，不发请求。"""
        assert _fetch_feed_content({"name": "X", "url": ""}) is None


# ═══════════════════════════════════════════════════
#  GitHub 热榜板块测试
# ═══════════════════════════════════════════════════

from digest.github import _select_five, _search_repos, _summarize_repos_zh, pick_github_trending  # noqa: E402
from digest.storage import load_sent_github_repos, save_sent_github_repos  # noqa: E402


def _make_repo(full_name: str, stars: int = 1000, kind: str = "rising",
               language: str = "Python", desc: str = "A test repo") -> dict:
    """快捷构造测试用 repo dict。"""
    return {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "description": desc,
        "stargazers_count": stars,
        "language": language,
        "kind": kind,
    }


class TestSelectFive:
    """_select_five 纯函数测试——不依赖任何 IO。"""

    def test_normal_3_rising_2_veteran(self):
        """黑马足量 → 3 黑马 + 2 老牌。"""
        rising = [_make_repo(f"owner/r{i}", stars=500 + i, kind="rising") for i in range(10)]
        veteran = [_make_repo(f"owner/v{i}", stars=10000 + i, kind="veteran") for i in range(10)]
        result = _select_five(rising, veteran, set())
        assert len(result) == 5
        kinds = [r["kind"] for r in result]
        assert kinds.count("rising") == 3
        assert kinds.count("veteran") == 2

    def test_rising_shortage_filled_by_veteran(self):
        """黑马不足 → 老牌补满 5。"""
        rising = [_make_repo("owner/r0", kind="rising")]
        veteran = [_make_repo(f"owner/v{i}", stars=10000 + i, kind="veteran") for i in range(10)]
        result = _select_five(rising, veteran, set())
        assert len(result) == 5
        kinds = [r["kind"] for r in result]
        assert kinds.count("rising") == 1
        assert kinds.count("veteran") == 4

    def test_veteran_shortage_filled_by_rising(self):
        """老牌不足 → 黑马补满 5。"""
        rising = [_make_repo(f"owner/r{i}", stars=500 + i, kind="rising") for i in range(10)]
        veteran = [_make_repo("owner/v0", kind="veteran")]
        result = _select_five(rising, veteran, set())
        assert len(result) == 5
        kinds = [r["kind"] for r in result]
        assert kinds.count("rising") == 4
        assert kinds.count("veteran") == 1

    def test_both_shortage_returns_actual(self):
        """两边都少 → 返回实际有的。"""
        rising = [_make_repo("owner/r0", kind="rising")]
        veteran = [_make_repo("owner/v0", kind="veteran")]
        result = _select_five(rising, veteran, set())
        assert len(result) == 2

    def test_sent_filter_works(self):
        """去重集过滤生效：已推过的 full_name 不出现。"""
        rising = [_make_repo(f"owner/r{i}", kind="rising") for i in range(10)]
        veteran = [_make_repo(f"owner/v{i}", kind="veteran") for i in range(10)]
        sent = {"owner/r0", "owner/r1", "owner/v0"}
        result = _select_five(rising, veteran, sent)
        names = {r["full_name"] for r in result}
        assert "owner/r0" not in names
        assert "owner/r1" not in names
        assert "owner/v0" not in names

    def test_overlap_no_duplicate(self):
        """黑马与老牌 full_name 重叠时不重复计入。"""
        shared = _make_repo("owner/shared", stars=5000, kind="rising")
        rising = [shared] + [_make_repo(f"owner/r{i}", kind="rising") for i in range(5)]
        veteran = [
            _make_repo("owner/shared", stars=5000, kind="veteran"),
        ] + [_make_repo(f"owner/v{i}", kind="veteran") for i in range(5)]
        result = _select_five(rising, veteran, set())
        names = [r["full_name"] for r in result]
        assert names.count("owner/shared") == 1

    def test_empty_inputs(self):
        """全空输入 → 返回空列表。"""
        assert _select_five([], [], set()) == []


def test_github_section_uses_actual_repo_count():
    """候选不足五条时，标题必须如实反映实际条数。"""
    repos = [_make_repo(f"owner/r{i}") for i in range(4)]

    result = _insert_github_section("正文", repos)

    assert "## 🔥 GitHub 热榜 · 今日 4 选" in result
    assert "## 🔥 GitHub 热榜 · 今日 5 选" not in result


class TestSearchRepos:
    """_search_repos 异常安全测试——mock requests.get。"""

    def test_non_200_returns_empty(self, monkeypatch) -> None:
        """HTTP 非 200 → 返回 [] 不抛。"""
        class FakeResp:
            status_code = 403
            text = "rate limit exceeded"

        monkeypatch.setattr("digest.github.requests.get", lambda *a, **k: FakeResp())
        result = _search_repos("test")
        assert result == []

    def test_timeout_returns_empty(self, monkeypatch) -> None:
        """超时 → 返回 [] 不抛。"""
        def fake_get(*a, **k):
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr("digest.github.requests.get", fake_get)
        result = _search_repos("test")
        assert result == []

    def test_json_parse_error_returns_empty(self, monkeypatch) -> None:
        """JSON 解析失败 → 返回 [] 不抛。"""
        class FakeResp:
            status_code = 200

            def json(self):
                raise ValueError("bad json")

        monkeypatch.setattr("digest.github.requests.get", lambda *a, **k: FakeResp())
        result = _search_repos("test")
        assert result == []

    def test_normal_response(self, monkeypatch) -> None:
        """正常 200 响应 → 正确解析字段。"""
        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "items": [
                        {
                            "full_name": "owner/repo1",
                            "html_url": "https://github.com/owner/repo1",
                            "description": "An awesome repo",
                            "stargazers_count": 1234,
                            "language": "Rust",
                        }
                    ]
                }

        monkeypatch.setattr("digest.github.requests.get", lambda *a, **k: FakeResp())
        result = _search_repos("test")
        assert len(result) == 1
        assert result[0]["full_name"] == "owner/repo1"
        assert result[0]["stargazers_count"] == 1234
        assert result[0]["language"] == "Rust"


class TestSummarizeReposZh:
    """_summarize_repos_zh AI 概括测试。"""

    def test_ai_failure_falls_back_to_english(self, monkeypatch) -> None:
        """AI 调用失败 → 回退用英文 description 截断。"""
        def fake_call(*a, **k):
            raise RuntimeError("DeepSeek down")

        monkeypatch.setattr("digest.github._call_deepseek_once", fake_call)
        repos = [
            _make_repo("owner/r1", desc="A" * 100),
            _make_repo("owner/r2", desc="B" * 100),
        ]
        result = _summarize_repos_zh(repos)
        assert result[0]["description_zh"] == "A" * 80
        assert result[1]["description_zh"] == "B" * 80

    def test_partial_parse_fills_fallback(self, monkeypatch) -> None:
        """AI 只返回 1 条 → 其余回退英文 description。"""
        def fake_call(*a, **k):
            return {"choices": [{"message": {"content": "1. 一个超棒的项目"}}]}

        monkeypatch.setattr("digest.github._call_deepseek_once", fake_call)
        repos = [
            _make_repo("owner/r1", desc="English desc one"),
            _make_repo("owner/r2", desc="English desc two"),
        ]
        result = _summarize_repos_zh(repos)
        assert result[0]["description_zh"] == "一个超棒的项目"
        assert result[1]["description_zh"] == "English desc two"

    def test_empty_repos_returns_empty(self):
        """空列表 → 原样返回。"""
        assert _summarize_repos_zh([]) == []


class TestPickGithubTrending:
    """pick_github_trending 入口测试。"""

    def test_disabled_returns_none(self, monkeypatch) -> None:
        """GITHUB_ENABLED=False → 返回 None。"""
        monkeypatch.setattr("digest.github.GITHUB_ENABLED", False)
        result = pick_github_trending()
        assert result is None

    def test_all_api_failure_returns_none(self, monkeypatch) -> None:
        """全 API 失败 → 返回 None。"""
        monkeypatch.setattr("digest.github.GITHUB_ENABLED", True)

        def fake_search(*a, **k):
            return []

        monkeypatch.setattr("digest.github._search_repos", fake_search)
        result = pick_github_trending()
        assert result is None


class TestGithubStorage:
    """load/save_sent_github_repos 去重存储测试。"""

    def test_load_nonexistent_file_returns_empty(self, monkeypatch) -> None:
        """文件不存在 → 返回空 set。"""
        monkeypatch.setattr("digest.storage.SENT_GITHUB_FILE",
                            "/nonexistent/path/gh_repos.json")
        result = load_sent_github_repos()
        assert result == set()

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch) -> None:
        """写入后读取 → 能正确取回。"""
        import json as _json
        test_file = tmp_path / "sent_github_repos.json"
        monkeypatch.setattr("digest.storage.SENT_GITHUB_FILE", str(test_file))
        monkeypatch.setattr("digest.storage.GITHUB_RETENTION_DAYS", 30)

        save_sent_github_repos(["owner/a", "owner/b"])
        loaded = load_sent_github_repos()
        assert "owner/a" in loaded
        assert "owner/b" in loaded

    def test_damaged_file_returns_empty(self, tmp_path, monkeypatch) -> None:
        """损坏的 JSON 文件 → 返回空 set，不抛异常。"""
        test_file = tmp_path / "damaged.json"
        test_file.write_text("not valid json{{{", encoding="utf-8")
        monkeypatch.setattr("digest.storage.SENT_GITHUB_FILE", str(test_file))
        result = load_sent_github_repos()
        assert result == set()

    def test_old_format_migration(self, tmp_path, monkeypatch) -> None:
        """旧格式（repos 列表无 history）→ 正确迁移并读取。"""
        import json as _json
        test_file = tmp_path / "old_format.json"
        old_data = {"repos": ["owner/old1", "owner/old2"], "ts": "2026-06-10 08:00"}
        test_file.write_text(_json.dumps(old_data), encoding="utf-8")
        monkeypatch.setattr("digest.storage.SENT_GITHUB_FILE", str(test_file))
        monkeypatch.setattr("digest.storage.GITHUB_RETENTION_DAYS", 30)

        loaded = load_sent_github_repos()
        assert "owner/old1" in loaded
        assert "owner/old2" in loaded

        save_sent_github_repos(["owner/new1"])
        loaded2 = load_sent_github_repos()
        assert "owner/old1" in loaded2
        assert "owner/new1" in loaded2
