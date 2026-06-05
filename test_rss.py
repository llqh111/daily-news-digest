"""RSS 连通性测试 —— 看看哪些新闻源能正常抓到"""
import feedparser

feeds = [
    ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Guardian World", "https://www.theguardian.com/world/rss"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("NHK World", "https://www3.nhk.or.jp/nhkworld/en/news/rss.xml"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

print("RSS source connectivity test\n" + "=" * 50)

ok_count = 0
for name, url in feeds:
    try:
        feed = feedparser.parse(url)
        count = len(feed.entries)
        if count > 0:
            first = feed.entries[0].get("title", "?")
            print(f"  [OK] {name}: {count} articles, latest: {first[:60]}")
            ok_count += 1
        else:
            print(f"  [WARN] {name}: 0 articles (blocked or feed changed)")
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")

print(f"\nResult: {ok_count}/{len(feeds)} feeds working")
