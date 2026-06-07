"""核心业务逻辑单元测试 —— 打分 & 聚类去重

运行: python -m pytest test_core.py -v
"""

import sys
import os
from datetime import datetime, timezone, timedelta

# 确保能 import main（测试脚本不在项目根目录也能跑）
sys.path.insert(0, os.path.dirname(__file__))

from main import (
    score_importance,
    title_keywords,
    extract_proper_nouns,
    same_story,
    cluster_and_boost,
    sanity_check_output,
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
