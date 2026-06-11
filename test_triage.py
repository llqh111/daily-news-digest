"""digest/triage.py 单测——全程 mock LLM，无真实网络调用。"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from digest.triage import triage_with_deepseek


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


def test_truncates_to_final_pick():
    """LLM 全部 keep=true 且超过 FINAL_PICK(13) → 截断到 13 条。"""
    articles = _make_articles(20)
    decisions = [{"id": i, "keep": True, "score": 20 - i, "reason": "ok"} for i in range(1, 21)]

    with patch("digest.triage._call_deepseek_once", _fake_llm(decisions)):
        result = triage_with_deepseek(articles)

    assert len(result) == 13


def test_all_keep_false_falls_back():
    """LLM 全部 keep=false → 回退粗筛排序。"""
    articles = _make_articles(5)
    decisions = [{"id": i, "keep": False, "score": 1, "reason": "差"} for i in range(1, 6)]

    with patch("digest.triage._call_deepseek_once", _fake_llm(decisions)):
        result = triage_with_deepseek(articles)

    # 回退到粗筛排序，应有条目
    assert len(result) > 0
    assert all("title" in a for a in result)
