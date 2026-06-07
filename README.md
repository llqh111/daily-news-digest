# 📰 每日全球要闻推送

每天两次，从 20 家全球权威媒体自动抓新闻 → DeepSeek AI 用中文写成深度晨报 → 推到你的微信和 Telegram。全程在 GitHub Actions 免费跑，你什么都不用做，坐等收推送。

## 它能做什么

- **自动抓取**：国际要闻（BBC/DW/AP/Reuters/日经/半岛/卫报/南华早报）+ 科技（MIT Tech Review/Hacker News/Ars Technica/The Verge/TechCrunch/Wired/36kr）+ 财经（FT/CNBC/CoinDesk/彭博）
- **智能筛选**：按关键词重要性 + 来源可信度 + 新鲜度 + 标题党识别，多层打分，只筛选真正值得看的
- **同题去重**：多家媒体报道同一事件自动合并，标出"被 N 家同时报道"，一条吃掉所有重复
- **抓正文写深度**：去新闻原网页提取文章全文（非 RSS 摘要）→ 喂给 AI 写出有来龙去脉、敢下判断的深度简报
- **三层防幻觉**：跨文章数字交叉比对 → 提示词强制自我审计 → 输出端扫描未来日期/编造数字
- **多渠道推送**：微信（Server酱）+ Telegram，海外跑在 GitHub Actions 上不怕墙
- **失败告警**：任一环出错，立刻推故障通知到你所有可用通道
- **跨天去重**：早晚报不会重复同一条新闻

## 你是怎么收到的

```
RSS源(20家) → 时间/关键词/来源打分 → 同题聚类去重 → 抓正文全文
→ DeepSeek AI 写成中文晨报 → 微信/Telegram 推送
```

## 快速开始

### 你需要准备

- GitHub 账号（用 Actions 定时跑）
- DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com) 注册，充值 10 块能用几个月）
- Server酱 SendKey（[sct.ftqq.com](https://sct.ftqq.com) 注册 → 微信扫码绑定 → 拿到 SendKey）
- （可选）Telegram Bot Token + Chat ID（海外备通道，防 Server酱 被墙）

### 1. 克隆项目

```bash
git clone https://github.com/你的用户名/daily-news-digest.git
cd daily-news-digest
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 Key：

```ini
DEEPSEEK_API_KEY=sk-你的deepseek-key
SERVERCHAN_SENDKEY=SCT你的sendkey
# 多人推送用逗号分隔：
# SERVERCHAN_SENDKEY=SCT你的key,SCT朋友的key

# 可选：Telegram
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=你的数字ID
```

### 3. 本地跑一次验证

```bash
pip install -r requirements.txt
python main.py
```

如果一切正常，你的微信/Telegram 应该收到一份晨报。

### 4. 在 GitHub 上设置自动运行

1. Fork 这个仓库
2. 在 `Settings → Secrets and variables → Actions` 里添加 4 个 Secret：
   - `DEEPSEEK_API_KEY`
   - `SERVERCHAN_SENDKEY`
   - `TELEGRAM_BOT_TOKEN`（可选）
   - `TELEGRAM_CHAT_ID`（可选）
3. Actions 默认启用，每天北京时间 **8:00 和 20:00** 自动推
4. 也可以在 Actions 页面点 `Run workflow` 手动触发

> ⚠️ **重要**：Actions 写了 `contents: write` 权限，因为它每次跑完要自动提交 `sent_articles.json`（记录已推送的链接，防止早晚报重复）。

## 自定义你的晨报

所有可调的参数都在 `main.py` 顶部的**配置区**里，有详细注释：

### 想改新闻源

```python
RSS_FEEDS = [
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    # 加新源：加一行字典，name 随便取，url 填 RSS 地址，category 填分类
]
```

### 想调"什么算重要新闻"

三个关键词表，全部用小写：

```python
HIGH_SIGNAL_KEYWORDS = ["war", "ceasefire", "nuclear", ...]   # 命中 +3 分
MEDIUM_SIGNAL_KEYWORDS = ["ai", "apple", "earnings", ...]     # 命中 +1 分
LOW_VALUE_KEYWORDS = ["celebrity", "quiz", "gossip", ...]     # 命中 -2 分
```

### 想让自己更信任的媒体优先

```python
SOURCE_TRUST = {
    "Reuters": 2,     # +2 分
    "The Verge": 1,   # +1 分
    # 没列的默认 0 分
}
```

### 其他可调项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_PER_FEED` | 8 | 每个源最多取几条 |
| `TIME_WINDOW_HOURS` | 24 | 只保留多少小时内的新闻 |
| `CANDIDATE_POOL` | 15 | 拣选多少条候选给 AI |
| `MIN_PER_CATEGORY` | `{"国际":6, "科技":4, "财经":4}` | 每类至少保留几条 |
| `FULLTEXT_MAX_CHARS` | 1000 | 每条正文最多取多少字（控 token 成本） |
| `BATCH_SIZE` | 7 | 每批最多喂给 AI 几条（超了自动拆批） |

## 诊断 & 测试

项目带了两个辅助脚本：

```bash
# 全面诊断：逐项检查 .env → DeepSeek API → Server酱 → RSS 源
python diagnose.py

# RSS 连通性测试：看看哪些新闻源还活着（直接用 main.py 里的源列表）
python test_rss.py
```

## 项目结构

```
daily-news-digest/
├── main.py              # 主程序（抓取 → 打分 → AI 总结 → 推送）
├── test_rss.py          # RSS 源连通性测试
├── diagnose.py          # 全链路诊断（API / 推送 / RSS）
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量模板
├── .gitignore
├── sent_articles.json   # 推送记录（自动生成，防重复）
├── .github/workflows/
│   └── daily-digest.yml # GitHub Actions 定时任务
└── 运行.bat / 诊断.bat   # Windows 双击运行批处理
```

## 常见问题

**Q: 为什么有些新闻源抓不到正文？**
A: 路透/AP/彭博没有公开 RSS，项目用 Google News 代理。它们的摘要仍会参与打分，但正文标注"仅摘要"。

**Q: 微信收不到？**
A: GitHub Actions 海外机器连国内 Server酱 偶尔掉包。项目已内置 3 次重试 + Telegram 备通道。

**Q: 怎么加人一起收？**
A: Server酱支持多个 SendKey 用逗号隔开：`SERVERCHAN_SENDKEY=SCT你的key,SCT朋友的key`。

**Q: DeepSeek 挂了怎么办？**
A: 目前该项目无降级方案（下一阶段计划）。失败会收到告警推送。

**Q: 能换别的 AI 吗？**
A: 能。`_call_deepseek_once()` 里改 API 地址和模型名就行。推荐用兼容 OpenAI 接口格式的模型（Gemini/Claude/GPT 都能改）。

## 补充文案

本项目深度使用大模型——如果你对 AI 技术感兴趣，可以访问 [Claude Code](https://claude.ai/claude-code) 了解更多。
