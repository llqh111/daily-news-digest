"""RSS 连通性测试 —— 看看哪些新闻源能正常抓到

直接复用 main.py 里的 RSS_FEEDS，所以你在 main.py 改了源，这里自动跟着测，不会脱节。
"""
import feedparser

from main import RSS_FEEDS

UA = "Mozilla/5.0 (compatible; DailyDigestBot/1.0)"

print("RSS source connectivity test\n" + "=" * 50)

ok_count = 0
for feed_info in RSS_FEEDS:
    name = feed_info["name"]
    tag = " [参考源/Google News]" if feed_info.get("reference") else ""
    try:
        feed = feedparser.parse(feed_info["url"], agent=UA)
        count = len(feed.entries)
        if count > 0:
            first = feed.entries[0].get("title", "?")
            print(f"  [OK] {name}{tag}: {count} articles, latest: {first[:60]}")
            ok_count += 1
        else:
            print(f"  [WARN] {name}{tag}: 0 articles (blocked or feed changed)")
    except Exception as e:
        print(f"  [FAIL] {name}{tag}: {e}")

print(f"\nResult: {ok_count}/{len(RSS_FEEDS)} feeds working")
