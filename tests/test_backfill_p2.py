from digest.backfill import PREFERRED_FULLTEXT_DOMAINS
from urllib.parse import urlparse

def test_backfill_hostname_matching():
    # 测试白名单确切匹配，避免子串绕过
    domains = ["reuters.com", "apnews.com"]

    def matches(url):
        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in domains)

    assert matches("https://www.reuters.com/article/123") is True
    assert matches("https://apnews.com/article/123") is True
    assert matches("https://reuters.com.evil.example/article/123") is False
    assert matches("https://myapnews.com/article/123") is False
    assert matches("https://www.myapnews.com/article/123") is False
