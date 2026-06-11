"""digest/search_provider.py 单测——全程 mock，无真实网络调用。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

import digest.config as cfg
from digest.search_provider import _reset_counter, web_search


# ──────────────────────────── 辅助 ────────────────────────────

def _make_response(status: int, json_data: dict) -> MagicMock:
    """构造一个模拟 requests.Response。"""
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = json_data
    if status >= 400:
        http_err = requests.exceptions.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = http_err
    else:
        mock_resp.raise_for_status.return_value = None
    return mock_resp


EXA_RESULT = {
    "results": [{
        "title": "Exa Title",
        "url": "https://exa.ai/article",
        "text": "Exa snippet text",
        "publishedDate": "2024-01-01",
    }]
}

TAVILY_RESULT = {
    "results": [{
        "title": "Tavily Title",
        "url": "https://tavily.com/article",
        "content": "Tavily snippet content",
        "published_date": "2024-01-02",
    }]
}


# ──────────────────────────── 测试 ────────────────────────────

def test_exa_success(monkeypatch):
    """Exa 成功 → 归一化 5 个 key，source == exa。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", "exa-key")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", None)

    with patch("requests.post", return_value=_make_response(200, EXA_RESULT)):
        results = web_search("test query")

    assert len(results) == 1
    r = results[0]
    assert set(r.keys()) == {"title", "url", "snippet", "published", "source"}
    assert r["source"] == "exa"
    assert r["title"] == "Exa Title"
    assert r["snippet"] == "Exa snippet text"
    assert r["published"] == "2024-01-01"


def test_exa_402_falls_back_to_tavily(monkeypatch):
    """Exa 返回 402 → 降级 Tavily，source == tavily。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", "exa-key")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", "tavily-key")

    responses = [
        _make_response(402, {}),
        _make_response(200, TAVILY_RESULT),
    ]
    with patch("requests.post", side_effect=responses):
        results = web_search("test")

    assert len(results) == 1
    assert results[0]["source"] == "tavily"
    assert results[0]["title"] == "Tavily Title"


def test_exa_429_falls_back_to_tavily(monkeypatch):
    """Exa 返回 429 → 降级 Tavily。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", "exa-key")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", "tavily-key")

    responses = [
        _make_response(429, {}),
        _make_response(200, TAVILY_RESULT),
    ]
    with patch("requests.post", side_effect=responses):
        results = web_search("test")

    assert results[0]["source"] == "tavily"


def test_exa_network_exception_falls_back_to_tavily(monkeypatch):
    """Exa 网络异常 → 降级 Tavily。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", "exa-key")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", "tavily-key")

    def side_effect(*args, **kwargs):
        # 第一次调用抛异常（Exa），第二次成功（Tavily）
        if side_effect.call_count == 0:
            side_effect.call_count += 1
            raise requests.exceptions.ConnectionError("network down")
        return _make_response(200, TAVILY_RESULT)

    side_effect.call_count = 0
    with patch("requests.post", side_effect=side_effect):
        results = web_search("test")

    assert results[0]["source"] == "tavily"


def test_no_keys_returns_empty(monkeypatch):
    """两个 key 都没配 → 返回 []，不调用网络。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", None)
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", None)

    with patch("requests.post") as mock_post:
        results = web_search("test")
        mock_post.assert_not_called()

    assert results == []


def test_both_providers_fail_returns_empty(monkeypatch):
    """Exa 和 Tavily 都挂掉 → 返回 []，不抛异常。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", "exa-key")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", "tavily-key")

    with patch("requests.post", side_effect=requests.exceptions.ConnectionError("fail")):
        results = web_search("test")

    assert results == []


def test_monthly_cap_returns_empty(monkeypatch):
    """月调用上限触发 → 直接返回 []，不访问网络。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", "exa-key")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", "tavily-key")
    monkeypatch.setattr(cfg, "SEARCH_MONTHLY_CAP", 0)  # 上限设为 0 触发

    with patch("requests.post") as mock_post:
        results = web_search("test")
        mock_post.assert_not_called()

    assert results == []


def test_missing_fields_normalized_to_empty_string(monkeypatch):
    """provider 结果缺字段 → 仍然返回完整 5 个 key，缺失字段为空串。"""
    _reset_counter()
    monkeypatch.setattr(cfg, "EXA_API_KEY", "exa-key")
    monkeypatch.setattr(cfg, "TAVILY_API_KEY", None)

    sparse = {"results": [{"url": "https://example.com"}]}  # 只有 url，其余缺失
    with patch("requests.post", return_value=_make_response(200, sparse)):
        results = web_search("test")

    assert len(results) == 1
    r = results[0]
    assert set(r.keys()) == {"title", "url", "snippet", "published", "source"}
    assert r["title"] == ""
    assert r["snippet"] == ""
    assert r["published"] == ""
    assert r["url"] == "https://example.com"
