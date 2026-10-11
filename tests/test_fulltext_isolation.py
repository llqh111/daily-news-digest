import subprocess
from types import SimpleNamespace

import pytest

from digest import fetch


@pytest.mark.parametrize("failure", ["abort", "timeout"])
def test_parser_failure_does_not_abort_digest(monkeypatch, failure):
    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: SimpleNamespace(
        text="<html>新闻素材</html>", raise_for_status=lambda: None,
    ))

    def parse(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return SimpleNamespace(returncode=-6, stdout="损坏正文")

    monkeypatch.setattr(fetch.subprocess, "run", parse)
    article = {"link": "https://example.com", "title": "新闻", "source": "来源", "score": 5}
    fetch.attach_fulltexts([article])
    assert article["fulltext"] == ""
    assert article["score"] == 5


def test_real_parser_subprocess_preserves_chinese(monkeypatch):
    paragraph = "这是一篇用于验证正文解析的中文报道，介绍新闻的事实与背景。" * 20
    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: SimpleNamespace(
        text=f"<html><body><article><h1>新闻标题</h1><p>{paragraph}</p></article></body></html>",
        raise_for_status=lambda: None,
    ))
    result = fetch.fetch_one_fulltext({"link": "https://example.com", "title": "新闻", "source": "来源"})
    assert paragraph in result
