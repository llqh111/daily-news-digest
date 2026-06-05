"""全面诊断 —— 逐项检测每个环节"""
import os, sys

print("=" * 55)
print("  每日新闻推送 - 诊断工具")
print("=" * 55)

# ── 1. 检查 .env ──
print("\n[1/4] 检查配置文件...")
from dotenv import load_dotenv
load_dotenv()
dk = os.getenv("DEEPSEEK_API_KEY", "")
sc = os.getenv("SERVERCHAN_SENDKEY", "")
if not dk:
    print("  FAIL: DEEPSEEK_API_KEY 没设置！")
    sys.exit(1)
if not sc:
    print("  FAIL: SERVERCHAN_SENDKEY 没设置！")
    sys.exit(1)
print(f"  OK: DeepSeek Key ({len(dk)} chars), Server酱 Key ({len(sc)} chars)")

# ── 2. 测试 DeepSeek API ──
print("\n[2/4] 测试 DeepSeek API...")
import requests
try:
    r = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {dk}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "说一句你好"}], "max_tokens": 20},
        timeout=30,
    )
    if r.status_code == 200:
        reply = r.json()["choices"][0]["message"]["content"]
        print(f"  OK: DeepSeek 回复 -> {reply}")
    else:
        print(f"  FAIL: HTTP {r.status_code} -> {r.text[:200]}")
        sys.exit(1)
except Exception as e:
    print(f"  FAIL: {e}")
    sys.exit(1)

# ── 3. 测试 Server酱 ──
print("\n[3/4] 测试 Server酱 推送...")
try:
    r = requests.post(
        f"https://sctapi.ftqq.com/{sc}.send",
        data={"title": "测试消息", "desp": "如果你看到这条消息，说明推送通道正常！"},
        timeout=30,
    )
    result = r.json()
    if result.get("code") == 0:
        print("  OK: 推送成功！请查看微信是否收到「测试消息」")
    else:
        print(f"  FAIL: Server酱返回 -> {result}")
        sys.exit(1)
except Exception as e:
    print(f"  FAIL: {e}")
    sys.exit(1)

# ── 4. 测试 RSS 抓取 ──
print("\n[4/4] 测试 RSS 抓取...")
import feedparser
feeds = [
    ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
]
for name, url in feeds:
    feed = feedparser.parse(url)
    print(f"  OK {name}: {len(feed.entries)} articles")
    if feed.entries:
        print(f"    Latest: {feed.entries[0].get('title', '?')[:60]}")

print("\n" + "=" * 55)
print("  全部检测通过！可以运行 main.py 了")
print("=" * 55)
