# GitHub 热榜独立板块 — 设计文档

- **日期**：2026-06-14
- **模块**：`digest/github.py`（新增）
- **目标**：每期固定推 5 个 GitHub 仓库（3 新晋黑马 + 2 活跃老牌），N 天内按 repo 全名去重，DeepSeek 批量写中文一句话，由 `main.py` 插入 `🔥 GitHub 热榜` 板块。

---

## 1. 背景与定位

项目现有结构：RSS 抓取 → 打分 → 聚类去重 → triage 精选 → 全文抓取 → DeepSeek 写中文简报 → 多渠道推送（微信 / Telegram）。除主新闻三大类（国际 / 科技 / 财经）外，已有两个**独立板块**先例：

- `digest/bio.py`：生物前沿单槽，每期精确 1 条，与主打分完全隔离，任何异常返回 `None` 不拖垮主流程。
- `digest/scout.py`：信息差侦察兵，独立成板块。

本设计**仿照 `bio.py` 模式**新增 GitHub 热榜板块，与主新闻打分完全隔离。

**与现有 GitHub Trending RSS 的关系**：`config.py` 的 `RSS_FEEDS` 已有一条 `GitHub Trending`（`https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml`，[config.py:159](../../../digest/config.py)），但它只是混进「科技」类参与**通用打分**，不保证条数、不去重、不独立成板块。本设计的新板块与它**互不影响**，那条 RSS **保留不动**。

---

## 2. 关键决策（已与用户确认）

| 决策点 | 选定方案 | 理由 |
|---|---|---|
| 数据源 | **GitHub 官方 Search API** | 官方、稳定、结构化字段全；与项目抓 RSS 的稳定性哲学一致。官方 trending 页面无 API，爬 HTML 易坏 |
| 「热榜」口径 | **混合**：黑马（新建高星）+ 老牌（活跃高星） | 兼顾新鲜爆款与知名项目动态 |
| 名额配比 | **3 黑马 + 2 老牌**，不够互补满 5 | 保证每期稳定 5 条 |
| 去重范围 | **N 天滚动窗口**，按 **repo 全名** | repo 隔 N 天再火重推合理；全名比 URL 干净（无 querystring） |
| 去重存储 | **独立文件** `sent_github_repos.json` | 新闻按 URL、repo 按 full_name，语义不同，不混入 `sent_articles.json` |
| 每个 repo 展示 | **AI 批量写中文一句话** | 与 bio 体验一致，中文读者友好；5 个一次调用成本极低 |
| 出现频次 | **早晚两班都出** | 与 bio / 信息差一致，靠去重保证早晚不重样，无需班次判断 |
| token | GitHub Actions 自动注入 `secrets.GITHUB_TOKEN`；本地匿名兜底 | 零配置；每次仅 2 请求，远不触限 |

---

## 3. 架构

新增单一模块 `digest/github.py`，对外只暴露一个入口函数：

```python
def pick_github_trending() -> list[dict] | None:
    """挑 5 个 trending 仓库（3 黑马 + 2 老牌，不够互补），附 AI 中文一句话。

    返回 [{full_name, url, description_zh, stars, language, kind}, ...]
    或 None（被关闭 / 无候选 / 任何异常）。kind ∈ {"rising", "veteran"}。
    """
```

容错总原则（与 bio 一致）：**任何异常 → 返回 `None`，绝不抛给主流水线**。

数据流：

```
拉黑马 (Search API) ┐
                    ├→ 过滤去重集 → 选 3 黑马 / 2 老牌（不够互补满 5）
拉老牌 (Search API) ┘     ↓
                    AI 批量写中文一句话（失败回退英文 description）
                         ↓
                    返回 list[dict] → main.py 渲染插入板块
                         ↓（推送成功后）
                    写入 sent_github_repos.json 去重记录
```

---

## 4. 模块内部组件

`digest/github.py` 拆为以下函数（每个职责单一、可单测）：

### 4.1 `_search_repos(query: str, per_page: int) -> list[dict]`
- 调 `GET https://api.github.com/search/repositories`，带 `q` / `sort=stars` / `order=desc` / `per_page`。
- Header：`Accept: application/vnd.github+json`；若环境有 `GITHUB_TOKEN` 则加 `Authorization: Bearer ...`。
- 超时 `GITHUB_TIMEOUT`。失败 / 非 200 / 解析异常 → 返回 `[]`（不抛）。
- 抽字段：`full_name`、`html_url`、`description`、`stargazers_count`、`language`。

### 4.2 `_collect_candidates() -> tuple[list[dict], list[dict]]`
- 构造黑马查询：`created:>{今-RISING_DAYS} stars:>={RISING_MIN_STARS}`。
- 构造老牌查询：`pushed:>{今-VETERAN_DAYS} stars:>={VETERAN_MIN_STARS}`。
- 日期用北京时区 `TZ` 推算，格式 `YYYY-MM-DD`。
- 各取 `per_page`（建议 `quota×2 + 余量`，给去重过滤留候选）。
- 返回 `(rising_candidates, veteran_candidates)`，各自标 `kind`。

### 4.3 `_select_five(rising, veteran, sent: set[str]) -> list[dict]`
- 纯函数（无 IO），便于单测。
- 先过滤掉 `full_name` 在 `sent` 里的。
- 取前 `QUOTA["rising"]` 个黑马；老牌过滤掉 `sent` + 已选黑马的 full_name，取前 `QUOTA["veteran"]` 个。
- 若总数 < 5：用另一边剩余候选补满（黑马不足用老牌补，反之亦然），仍按 stars 降序。
- 上限 5；若两边合计 < 5 则返回实际有的（可能少于 5，极端情况）。

### 4.4 `_summarize_repos_zh(repos: list[dict]) -> list[dict]`
- 5 个 repo 拼一个 prompt，一次 `_call_deepseek_once(model=GITHUB_MODEL)`。
- system prompt：要求逐个输出中文一句话（≤30 字，「做什么 + 为啥火」），按编号对应。
- 解析模型输出，写回每个 repo 的 `description_zh`。
- 失败 / 解析不齐 → 该 repo 回退用原始英文 `description` 截断（≤80 字）。

### 4.5 `pick_github_trending() -> list[dict] | None`
- 入口。`GITHUB_ENABLED` 关则返回 `None`。
- 编排 4.2 → 加载去重集 → 4.3 → 4.4 → 返回。
- **注意**：去重记录的写入**不在此处**，而在 `main.py` 推送成功后才写（与新闻一致，保证失败可重试）。

---

## 5. 去重层（复用 `storage.py` 模式）

新增独立文件 `sent_github_repos.json`，结构与 `sent_articles.json` 同构（按天分桶 + N 天窗口）：

```json
{
  "updated": "2026-06-14 08:01",
  "retention_days": 30,
  "history": {
    "2026-06-14": ["owner/repoA", "owner/repoB"]
  }
}
```

在 `storage.py` 新增两个函数（参数化文件路径，复用现有按天分桶 + 过期清理逻辑）：

```python
def load_sent_github_repos() -> set[str]: ...      # 合并近 RETENTION 天 full_name → flat set
def save_sent_github_repos(full_names: list[str]) -> None: ...  # 按天归档 + 清理旧天
```

实现要点：
- 复用 `storage.py` 已有的「兼容旧格式 / 按天分桶 / 超 `GITHUB_RETENTION_DAYS` 天清理」逻辑（可抽公共助手，或照 `load_sent_links`/`save_sent_links` 平行实现）。
- 异常一律「读不到当无历史」，绝不拖垮主流程。

---

## 6. 接入 `main.py`

仿 `_insert_bio_section`，新增渲染 + 编排两处改动：

### 6.1 渲染函数 `_insert_github_section(summary, repos)`
插在「编辑手记」/「自我审计」前（找不到 marker 则追加末尾）。板块样式：

```markdown
## 🔥 GitHub 热榜 · 今日 5 选

### 1. owner/repo  ⭐ 12.3k · Python  🚀新晋
做什么 + 为啥火的一句话中文。
🔗 https://github.com/owner/repo
```

- 角标：`kind=="rising"` → `🚀新晋`；`kind=="veteran"` → `🏛️老牌`。
- 星数格式化：≥1000 显示 `12.3k`，否则原数。
- `language` 为空则省略该段。

### 6.2 主流程编排
- 在 bio 之后加：
  ```python
  log.info("🔥 GitHub 热榜挑选（github）...")
  repos = pick_github_trending()
  log.info("GitHub 板块：" + (f"已选 {len(repos)} 条" if repos else "本期无"))
  ```
- 在插入 bio 板块后加：`summary = _insert_github_section(summary, repos)`。
- **推送成功后**（`save_sent_links` 附近）写去重记录：
  ```python
  if repos:
      save_sent_github_repos([r["full_name"] for r in repos])
  ```
- `stage_map` 加 `"pick_github_trending": "GitHub热榜"`，便于失败定位。
- `main.py` 顶部兼容 re-export 加 `from digest.github import pick_github_trending`。

---

## 7. 配置项（加到 `config.py`）

```python
# ═══════════════════════════════════════════════════
#  GitHub 热榜板块（独立板块，每期 5 条，不参与主新闻打分竞争）
# ═══════════════════════════════════════════════════
GITHUB_ENABLED = True                          # 板块总开关
GITHUB_MODEL = "deepseek-chat"                 # 中文一句话用 V3（便宜够用）
GITHUB_QUOTA = {"rising": 3, "veteran": 2}     # 黑马/老牌配比，和 = 5
GITHUB_RISING_DAYS = 7                          # 黑马：created 近 N 天
GITHUB_VETERAN_DAYS = 30                        # 老牌：pushed 近 N 天
GITHUB_RISING_MIN_STARS = 50                    # 黑马最低星
GITHUB_VETERAN_MIN_STARS = 5000                 # 老牌最低星
GITHUB_RETENTION_DAYS = 30                      # repo 去重窗口（比新闻 7 天长）
GITHUB_TIMEOUT = 12                             # Search API 单请求超时（秒）
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")        # 可选；GitHub Actions 自动注入

# 去重记录文件（独立于新闻的 sent_articles.json）
SENT_GITHUB_FILE = os.path.join(_PROJECT_ROOT, "sent_github_repos.json")
```

GitHub Actions workflow（`.github/workflows/daily-digest.yml`）需把 `GITHUB_TOKEN` 透传为环境变量：
```yaml
env:
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```
（`secrets.GITHUB_TOKEN` 由 Actions 自动提供，无需手动创建。）另需确保 `sent_github_repos.json` 与 `sent_articles.json` 一样被 commit 回仓库（workflow 已有 `contents: write`）。

---

## 8. 测试（`test_core.py`）

用 `monkeypatch` mock `requests.get`，不打真 API：

1. `_select_five`：黑马足量 → 3+2；黑马不足 → 老牌补满 5；两边都少 → 返回实际数。
2. `_select_five`：去重集过滤生效（已推过的 full_name 不出现）。
3. `_select_five`：黑马与老牌 full_name 重叠时不重复计入。
4. `_search_repos`：HTTP 非 200 / 超时 / JSON 解析失败 → 返回 `[]` 不抛。
5. `pick_github_trending`：`GITHUB_ENABLED=False` → `None`；全 API 失败 → `None`。
6. `_summarize_repos_zh`：AI 调用失败 → 每个 repo 回退英文 description 截断。
7. `load/save_sent_github_repos`：按天分桶、超窗口清理、读损坏文件当无历史。

目标：新增逻辑单测覆盖 ≥80%。

---

## 9. 风险与取舍

1. **口径近似**：Search API「黑马」≈ 但 ≠ 官方 trending 算法（已确认接受）。
2. **匿名限额**：本地无 `GITHUB_TOKEN` 时匿名 10 req/min；每次仅 2 请求，无碍。
3. **老牌轮播**：`sort=stars desc` + 30 天去重，一月内轮播 top 知名项目；池子（>5000 星近 30 天有更新）远大于 60，不枯竭。
4. **极端少于 5**：两边候选合计 < 5（API 异常）时板块可能 < 5 条或为空——容错优先，不报错。

---

## 10. 文件改动清单

| 文件 | 改动 |
|---|---|
| `digest/github.py` | **新增**：取数 / 选 5 / AI 概括 / 入口 |
| `digest/config.py` | **新增** GitHub 板块配置区 + `SENT_GITHUB_FILE` |
| `digest/storage.py` | **新增** `load_sent_github_repos` / `save_sent_github_repos` |
| `main.py` | **新增** `_insert_github_section` + 主流程编排 + re-export + stage_map |
| `.github/workflows/daily-digest.yml` | 透传 `GITHUB_TOKEN` 环境变量 |
| `test_core.py` | **新增** 上述单测 |
| `.gitignore` | 确认 `sent_github_repos.json` **不**被忽略（需 commit 回仓） |
