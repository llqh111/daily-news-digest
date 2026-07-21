"""自评重写环单元测试。

约束：不打真 DeepSeek API。所有 LLM 调用通过注入的 call / evaluate / revise 替身。
"""

from __future__ import annotations

from digest.critique import evaluate_digest, refine_digest


def _llm_returning(content: str):
    """伪 _call_deepseek_once：忽略入参，返回固定 content 的标准响应结构。"""
    def call(system, user, max_tokens=8000, model=""):
        return {"choices": [{"message": {"content": content}}], "usage": {}}
    return call


# ── 一份带 3 个来源标记的「成稿」样例 ──
_DIGEST = (
    "## 🌍 国际要闻\n"
    "**⭐⭐⭐ 标题一**\n【核心事实】xxx\n> 📰 来源：BBC（http://a）\n\n"
    "**⭐⭐ 标题二**\n【核心事实】yyy\n> 📰 来源：DW（http://b）\n\n"
    "**⭐ 标题三**\n【核心事实】zzz\n> 📰 来源：FT（http://c）\n"
)


class TestEvaluateDigest:
    def test_parse_failure_returns_pass(self):
        """裁判输出无法解析 → 视为通过（overall=10），绝不阻断推送。"""
        report = evaluate_digest(_DIGEST, call=_llm_returning("这不是 JSON"))
        assert report["overall"] >= 10
        assert report["issues"] == []

    def test_parses_structured_json(self):
        """能从输出里抠出 {overall, issues}。"""
        content = '随便夹带几句\n{"overall": 4, "issues": ["空话太多", "三段式塌了"]}\n收尾'
        report = evaluate_digest(_DIGEST, call=_llm_returning(content))
        assert report["overall"] == 4
        assert "空话太多" in report["issues"]

    def test_recovers_issues_with_unescaped_quotes(self):
        """模型常把引用中的双引号直接放进 JSON 字符串，仍应保留评分与问题。"""
        content = (
            '{"overall": 8, "issues": ['
            '"Story says "stop ships" without attribution.", '
            '"Second issue."]}'
        )

        report = evaluate_digest(_DIGEST, call=_llm_returning(content))

        assert report["overall"] == 8
        assert report["issues"] == [
            'Story says "stop ships" without attribution.',
            "Second issue.",
        ]


class TestRefineDigest:
    def test_disabled_passthrough(self):
        out = refine_digest(_DIGEST, enabled=False)
        assert out == _DIGEST

    def test_skips_rewrite_when_high_score(self):
        """高分（≥阈值 6.0）不触发重写。"""
        calls = {"revise": 0}

        def revise(summary, issues):
            calls["revise"] += 1
            return summary + "改写了"

        out = refine_digest(_DIGEST, enabled=True,
                            evaluate=lambda s: {"overall": 8, "issues": []},
                            revise=revise)
        assert calls["revise"] == 0
        assert out == _DIGEST

    def test_rewrites_when_low_score(self):
        """低分触发重写；重写稿保住来源数与长度 → 采用。"""
        improved = _DIGEST.replace("xxx", "更扎实的核心事实和数据")

        out = refine_digest(_DIGEST, enabled=True,
                            evaluate=lambda s: {"overall": 3, "issues": ["空话"]},
                            revise=lambda s, i: improved)
        assert out == improved

    def test_keeps_original_when_rewrite_drops_sources(self):
        """重写稿来源数下降（破格式风险）→ 弃用重写，回退原稿。"""
        broken = (
            "**标题一**\n【核心事实】xxx\n> 📰 来源：BBC（http://a）\n"
        )  # 只剩 1 个来源标记（原稿 3 个）
        out = refine_digest(_DIGEST, enabled=True,
                            evaluate=lambda s: {"overall": 3, "issues": ["x"]},
                            revise=lambda s, i: broken)
        assert out == _DIGEST

    def test_keeps_original_when_rewrite_too_short(self):
        """重写稿长度暴跌（<70%）→ 弃用。"""
        tiny = "> 📰 来源：BBC\n> 📰 来源：DW\n> 📰 来源：FT"  # 来源数够但内容塌缩
        out = refine_digest(_DIGEST, enabled=True,
                            evaluate=lambda s: {"overall": 3, "issues": ["x"]},
                            revise=lambda s, i: tiny)
        assert out == _DIGEST

    def test_revise_exception_falls_back(self):
        """重写调用抛异常 → 回退原稿，不崩。"""
        def boom(summary, issues):
            raise RuntimeError("LLM down")

        out = refine_digest(_DIGEST, enabled=True,
                            evaluate=lambda s: {"overall": 3, "issues": ["x"]},
                            revise=boom)
        assert out == _DIGEST
