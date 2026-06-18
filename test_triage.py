"""digest/triage.py 单测——全程 mock LLM，无真实网络调用。"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from digest.triage import triage_with_deepseek, _extract_decisions


# ──────────────────────────── 辅助 ────────────────────────────

def _make_articles(n: int) -> list[dict]:
    """生成 n 条模拟候选文章。"""
    return [
        {
            "title": f"新闻标题 {i}",
            "summary": f"这是第 {i} 条新闻的摘要。",
            "source": f"Source{i}",
            "category": "科技",
            "score": float(n - i),  # 粗筛分数递减
            "link": f"https://example.com/{i}",
        }
        for i in range(1, n + 1)
    ]


def _fake_llm(decisions: list[dict]):
    """返回一个模拟 _call_deepseek_once 的函数，输出指定的 decisions JSON。"""
    def fake(system_prompt, user_prompt, max_tokens=8000, model="deepseek-reasoner"):
        return {"choices": [{"message": {"content": json.dumps(decisions)}}]}
    return fake


# ──────────────────────────── 测试 ────────────────────────────

def test_basic_selection_and_sorting():
    """正常 JSON → 只保留 keep=true，按 score 降序，写回 ai_score/ai_reason。"""
    articles = _make_articles(5)
    decisions = [
        {"id": 1, "keep": True,  "score": 7, "reason": "重要事件"},
        {"id": 2, "keep": False, "score": 3, "reason": "软文"},
        {"id": 3, "keep": True,  "score": 9, "reason": "突发要闻"},
        {"id": 4, "keep": True,  "score": 5, "reason": "值得关注"},
        {"id": 5, "keep": False, "score": 2, "reason": "无价值"},
    ]
    with patch("digest.triage._call_deepseek_once", _fake_llm(decisions)):
        result = triage_with_deepseek(articles)

    assert len(result) == 3
    assert result[0]["ai_score"] == 9.0   # id=3 排第一
    assert result[1]["ai_score"] == 7.0   # id=1 排第二
    assert result[2]["ai_score"] == 5.0   # id=4 排第三
    assert result[0]["ai_reason"] == "突发要闻"
    # keep=false 的 id=2/5 不应出现
    titles = [a["title"] for a in result]
    assert "新闻标题 2" not in titles
    assert "新闻标题 5" not in titles


def test_json_in_markdown_fences():
    """LLM 把 JSON 包在 ```json ... ``` 里 → 正则依然能抠出来。"""
    articles = _make_articles(3)
    decisions = [
        {"id": 1, "keep": True, "score": 8, "reason": "好"},
        {"id": 2, "keep": True, "score": 6, "reason": "一般"},
        {"id": 3, "keep": True, "score": 4, "reason": "较弱"},
    ]
    raw = f"好的，以下是决策：\n```json\n{json.dumps(decisions)}\n```\n请参考。"

    def fake_with_prose(sp, up, max_tokens=8000, model="deepseek-reasoner"):
        return {"choices": [{"message": {"content": raw}}]}

    with patch("digest.triage._call_deepseek_once", fake_with_prose):
        result = triage_with_deepseek(articles)

    assert len(result) == 3
    assert result[0]["ai_score"] == 8.0


def test_malformed_json_falls_back():
    """JSON 损坏 → 回退粗筛排序，不抛异常，返回 ≤ FINAL_PICK 条。"""
    articles = _make_articles(20)

    def fake_bad(sp, up, max_tokens=8000, model="deepseek-reasoner"):
        return {"choices": [{"message": {"content": "这不是 JSON { broken"}}]}

    with patch("digest.triage._call_deepseek_once", fake_bad):
        result = triage_with_deepseek(articles)

    assert len(result) <= 13
    assert result[0]["score"] >= result[-1]["score"]  # 按粗筛 score 降序


def test_llm_raises_falls_back():
    """LLM 抛异常 → 回退粗筛排序，不抛异常。"""
    articles = _make_articles(10)

    def fake_raises(sp, up, max_tokens=8000, model="deepseek-reasoner"):
        raise RuntimeError("API timeout")

    with patch("digest.triage._call_deepseek_once", fake_raises):
        result = triage_with_deepseek(articles)

    assert len(result) <= 13
    assert all("title" in a for a in result)


def test_empty_input_returns_empty():
    """空输入 → 直接返回 []，不调用 LLM。"""
    with patch("digest.triage._call_deepseek_once") as mock_llm:
        result = triage_with_deepseek([])
        mock_llm.assert_not_called()

    assert result == []


def test_per_category_quota_cap():
    """新选稿模型：分类硬分桶。20 条同属「科技」且全 keep=true →
    最终只取科技配额上限 CATEGORY_QUOTA['科技'] 条（跨类零竞争，不再全局凑满 13）。"""
    from digest.config import CATEGORY_QUOTA

    articles = _make_articles(20)  # _make_articles 全部 category="科技"
    decisions = [{"id": i, "keep": True, "score": 20 - i, "reason": "ok"} for i in range(1, 21)]

    with patch("digest.triage._call_deepseek_once", _fake_llm(decisions)):
        result = triage_with_deepseek(articles)

    assert len(result) == CATEGORY_QUOTA["科技"], (
        f"科技封顶应为 {CATEGORY_QUOTA['科技']} 条，实际 {len(result)}"
    )
    # 应按 ai_score 降序取前 N（id=1 分最高）
    assert result[0]["ai_score"] == 19.0


def test_high_score_tech_cannot_displace_international():
    """解耦核心验证：科技条目分数再高，也吃不掉「国际」的名额——各排各的、跨类零竞争。

    旧版（全局按分数 top-13）会让 8 条高分科技霸占结果；新版按分类硬分桶后，
    科技封顶 CATEGORY_QUOTA['科技'] 条，国际/财经各自在本类内选满，互不挤占。
    """
    from digest.config import CATEGORY_QUOTA

    articles = (
        [{"title": f"科技{i}", "summary": "", "source": "S", "category": "科技",
          "score": 100.0, "link": f"t{i}"} for i in range(1, 9)]          # 8 条高分科技
        + [{"title": f"国际{i}", "summary": "", "source": "S", "category": "国际",
            "score": 1.0, "link": f"g{i}"} for i in range(1, 5)]           # 4 条低分国际
        + [{"title": f"财经{i}", "summary": "", "source": "S", "category": "财经",
            "score": 1.0, "link": f"c{i}"} for i in range(1, 3)]           # 2 条低分财经
    )
    # AI 全部保留：科技高分、国际/财经低分
    decisions = (
        [{"id": i, "keep": True, "score": 10, "reason": "x"} for i in range(1, 9)]
        + [{"id": i, "keep": True, "score": 3, "reason": "x"} for i in range(9, 15)]
    )
    with patch("digest.triage._call_deepseek_once", _fake_llm(decisions)):
        result = triage_with_deepseek(articles)

    cats: dict[str, int] = {}
    for a in result:
        cats[a["category"]] = cats.get(a["category"], 0) + 1

    assert cats.get("科技", 0) == CATEGORY_QUOTA["科技"], f"科技应封顶，实际分布 {cats}"
    assert cats.get("国际", 0) == 4, f"4 条国际应全入选、未被科技挤掉，实际 {cats}"
    assert cats.get("财经", 0) == 2, f"2 条财经应全入选，实际 {cats}"


def test_all_keep_false_falls_back():
    """LLM 全部 keep=false → 回退粗筛排序。"""
    articles = _make_articles(5)
    decisions = [{"id": i, "keep": False, "score": 1, "reason": "差"} for i in range(1, 6)]

    with patch("digest.triage._call_deepseek_once", _fake_llm(decisions)):
        result = triage_with_deepseek(articles)

    # 回退到粗筛排序，应有条目
    assert len(result) > 0
    assert all("title" in a for a in result)


# ──────────────────────────── _extract_decisions 抗截断解析 ────────────────────────────

def test_extract_clean_array():
    """正常裸数组应整体解析。"""
    out = _extract_decisions('[{"id":1,"keep":true,"score":8,"reason":"x"}]')
    assert out == [{"id": 1, "keep": True, "score": 8, "reason": "x"}]


def test_extract_code_block_array():
    """```json 代码块包裹的数组应解析。"""
    out = _extract_decisions('```json\n[{"id":2,"keep":false,"score":3,"reason":"y"}]\n```')
    assert len(out) == 1 and out[0]["id"] == 2


def test_extract_truncated_array_salvages_objects():
    """max_tokens 截断、数组缺闭合 ] → 逐对象救活，丢最后半条不整盘报废。"""
    # 第 3 个对象被截断（没写完也没 ]）
    truncated = (
        '[{"id":1,"keep":true,"score":9,"reason":"a"},'
        '{"id":2,"keep":true,"score":7,"reason":"b"},'
        '{"id":3,"keep":tr'
    )
    out = _extract_decisions(truncated)
    ids = [d["id"] for d in out]
    assert ids == [1, 2], f"应救活前 2 条完整对象，实际 {ids}"


def test_extract_garbage_returns_empty():
    """完全没有 JSON 对象 → 返回空列表（交由调用方 fallback）。"""
    assert _extract_decisions("抱歉，我无法完成这个任务。") == []
