"""Regression tests for deterministic batch-rendering cleanup."""

import pytest

from digest import ai

from digest.ai import _is_empty_section_placeholder


def test_empty_batch_section_placeholder_is_removed():
    assert _is_empty_section_placeholder("（本批暂无符合该板块的候选新闻。）")


def test_real_news_text_is_not_treated_as_placeholder():
    assert not _is_empty_section_placeholder("国际要闻：一项新协议今日生效")


def test_empty_thinking_stream_retries_without_thinking(monkeypatch):
    """推理流没有最终正文时，必须改用非思考模式重试一次。"""
    requests_payloads = []

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=True):
            if len(requests_payloads) == 1:
                yield 'data: {"choices": [{"delta": {"reasoning_content": "先推理"}}]}'
            else:
                yield 'data: {"choices": [{"delta": {"content": "最终正文"}}]}'
            yield "data: [DONE]"

    def fake_post(*args, **kwargs):
        requests_payloads.append(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr(ai.requests, "post", fake_post)

    result = ai._call_deepseek_once("system", "user")

    assert result["choices"][0]["message"]["content"] == "最终正文"
    assert [payload["thinking"]["type"] for payload in requests_payloads] == [
        "enabled", "disabled"
    ]


def test_empty_non_thinking_stream_raises(monkeypatch):
    """非思考模式仍为空时，调用不能被伪装成成功。"""
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=True):
            yield "data: [DONE]"

    monkeypatch.setattr(ai.requests, "post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(RuntimeError, match="空的最终正文"):
        ai._call_deepseek_once("system", "user", thinking_enabled=False)
