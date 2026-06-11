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
