# 设计文档：内容质量增强四件套

> 状态：草案 · 待评审
> 范围：`digest/` 内 4 个内容质量特性（承接 `DESIGN-semantic-dedup-and-self-critique.md` 第 3 节）
> 约束：**只有 DeepSeek API**（纯文本对话补全；无 embedding / 无视觉 / 无 TTS）；嵌入靠本地 `fastembed`

---

## 0. 背景与前置依赖

本文档把上一份设计（语义去重 + 自评重写）§3「内容质量还能往哪提升」里的四条展开成可实现方案：

1. 跨期事件串联做实
2. 成稿内近重复终检
3. 参考源深度天花板
4. 每条信息密度地板

**关键前置：特性一（语义去重）的嵌入基建尚未落地。** 现状盘点：

| 资产 | 状态 |
|------|------|
| `digest/embedding.py`（`embed_titles`） | ❌ 不存在 |
| `digest/scoring.py::merge_similar_clusters` | ❌ 不存在 |
| `test_semantic_dedup.py` | ✅ 已就位，但 `from digest.embedding import embed_titles` 当前 **import 失败** |
| `config.py` 语义/裁判配置键 | ❌ 无 |
| `requirements.txt` 的 `fastembed` | ❌ 无 |

**依赖关系**：

```
特性一嵌入基建 (embed_titles)  ← 必须先建
   ├── #1 跨期事件串联    （吃 embed_titles）
   ├── #2 成稿近重复终检  （吃 embed_titles）
   └── #4 信息密度地板    （A 版纯代码不依赖；B 版并入特性二 issues）
#3 参考源深度天花板        ← 独立，复用 scout 的 search+read，零新依赖
```

所以落地顺序：**先补齐 `embed_titles` + `merge_similar_clusters`（让 `test_semantic_dedup.py` 转绿）**，再做 #1/#2；#3 可任意时刻并行；#4 先上纯代码版摸阈值。

---

## 1. 跨期事件串联做实（高协同）

### 1.1 现状与缺口

- `storage.load_recent_digests()`（[storage.py:158](../digest/storage.py)）用正则 `\*\*[🔥⭐🎯📈].*?\*\*` 从近 3 期 `digests/*.md` 抠出**已渲染的中文标题**，纯文本塞进写作 prompt（[ai.py:248](../digest/ai.py)）。
- 是否「旧事件进展」全靠 DeepSeek 肉眼比对（prompt 规则见 [ai.py:51](../digest/ai.py)），易漏（措辞变了认不出）、易误（蹭到无关旧闻）。

### 1.2 核心坑：语言错配（必须先拍板）

今天的候选条目在写稿前是 **原始 feed 标题（多为英文）**；过去 digest 里留下的是 **译过的中文标题**。直接跨语言 embed 比对，e5-multilingual 虽支持但精度打折，会制造误命中/漏命中。

**干净解法：存档时落一份结构化 sidecar，把原始 rep 标题留住，跨期比「英文↔英文」。**

```python
# storage.save_digest_markdown 时顺手写 digests/meta/YYYY-MM-DD-AM.json
# {
#   "date": "2026-06-19", "session": "AM",
#   "reps": [
#     {"raw_title": "OpenAI launches GPT-5", "zh": "OpenAI 发布 GPT-5", "emoji": "🔥"}
#   ]
# }
```

> 决策点：sidecar 需要「raw_title」。但 `save_digest_markdown` 当前只拿到成稿 markdown，拿不到 reps。需在 `main.py` 把 reps（或其 title + 渲染后中文标题）一路带到存档调用，或在写稿前把 reps 暂存。**推荐**：写稿阶段产出 reps 时直接 dump sidecar（与 markdown 分离，互不影响）。

### 1.3 算法与落地点

新增 `digest/linkage.py`：

```python
def tag_progress(reps: list[dict]) -> None:
    """今天 reps 与近 N 期 sidecar raw_title 向量比，命中即写 art['progress_of']。
    embed 不可用 / 无历史 → 直接 return，回退现状（LLM 肉眼判），绝不中断。"""
    past = load_recent_reps()                          # storage 新增：读近 N 期 sidecar
    if not past:
        return
    today_vecs = embed_titles([r["title"] for r in reps])   # 复用特性一
    past_vecs = embed_titles([p["raw_title"] for p in past])
    if today_vecs is None or past_vecs is None:
        return
    sims = today_vecs @ past_vecs.T                    # 已 L2 归一化，点积=余弦
    for i, r in enumerate(reps):
        j = int(sims[i].argmax())
        if sims[i][j] >= PROGRESS_SIM_THRESHOLD:       # 比去重阈值松，~0.80
            r["progress_of"] = {"prev_zh": past[j]["zh"], "date": past[j]["date"]}
```

调用点：`main.py` 在 `summarize_with_deepseek(articles)`（[main.py:280](../main.py)）**之前**，对喂给 AI 的 `articles`（即 reps）跑一遍 `tag_progress`。

注入：[ai.py:99 `_articles_to_text`](../digest/ai.py) 内，若 `art.get("progress_of")` 就追加一行：

```
📈 此事曾于 {date} 报道为「{prev_zh}」，请按"进展"写（此前→现在→变化意味着什么）
```

prompt 已有 📈 规则，把「靠 LLM 找」降级成「代码喂确定的 prev/now 对」，幻觉面收窄。

### 1.4 配置（`config.py` 新增）

```python
PROGRESS_LINK_ENABLED = True
PROGRESS_SIM_THRESHOLD = 0.80      # 命中判进展（比去重阈值松，宁松勿漏）
PROGRESS_RECENT_DAYS = 3           # 回看几期 sidecar
```

### 1.5 测试（`test_linkage.py`）

- `embed_titles` 返回 None → `tag_progress` 不写任何 `progress_of`（降级）。
- 无 sidecar 历史 → 直接 return。
- mock 高相似向量 → 命中的 rep 写上正确 `progress_of`。
- 低相似 → 不写。
- sidecar 读写 round-trip。

### 1.6 风险

| 风险 | 缓解 |
|------|------|
| 语言错配误判 | sidecar 存 raw_title，英↔英比对 |
| 误标进展（蹭无关旧闻） | 阈值可配；prompt 仍保留「不确定就当新事件」兜底 |
| sidecar 写失败 | 与 markdown 存档解耦，try/except，失败=无历史 |

---

## 2. 成稿内近重复终检（高协同）

### 2.1 现状与缺口

去重只在抓取阶段（[scoring.py:162 `cluster_and_boost`](../digest/scoring.py)）。但 [main.py:285-289](../main.py) 的 `_insert_gap_section` / `_insert_bio_section` / `_insert_github_section` 是**成稿后硬拼**的独立板块，scout/bio 完全可能与主新闻撞题，去重逻辑根本没覆盖它们。

### 2.2 方案：检测 → 剔除次要板块的重复条（不重写、不合并）

合并要重过 LLM、破格式风险高，不值得。做法：**以主新闻标题为基准向量，scout/bio 命中近重复就剔除该条**（保主新闻，弃次要板块条目）。

新增 `digest/finalcheck.py`：

```python
def dedup_secondary(news_titles: list[str], gaps: list[dict],
                    bio: dict | None) -> tuple[list[dict], dict | None]:
    """主新闻标题为基准，剔除 scout/bio 中与之近重复的条目。
    embed 不可用 → 原样返回，绝不中断。"""
    if not news_titles:
        return gaps, bio
    base = embed_titles(news_titles)
    if base is None:
        return gaps, bio
    cand_titles = [g["title"] for g in gaps] + ([bio["title"]] if bio else [])
    cand = embed_titles(cand_titles)
    if cand is None:
        return gaps, bio
    sims = (cand @ base.T).max(axis=1)
    kept_gaps, k = [], 0
    for g in gaps:
        if sims[k] < FINAL_DEDUP_THRESHOLD:
            kept_gaps.append(g)
        else:
            log.info(f"终检剔除撞题 scout 条目：{g['title']}")
        k += 1
    kept_bio = bio
    if bio is not None and sims[k] >= FINAL_DEDUP_THRESHOLD:
        log.info(f"终检剔除撞题 bio 条目：{bio['title']}")
        kept_bio = None
    return kept_gaps, kept_bio
```

### 2.3 取基准标题：用 reps，别解析成稿（关键）

成稿是已渲染 markdown，标题埋在 `**🔥 …**` 行里。两条路：(a) 正则抠成稿；(b) **直接用 `reps` 的标题**当基准，不依赖解析。

**推荐 (b)**：在 `main.py` 调 `_insert_*` 之前，把进 AI 的 reps 标题列表传给 `dedup_secondary`，对 `gaps` / `bio` 过滤后再插板块。稳，不受成稿格式波动影响。

落地点：`main.py` 在 `gaps = scout_for_gaps()` / `bio = pick_bio_breakthrough()` 之后、`_insert_gap_section` 之前：

```python
news_titles = [a["title"] for a in articles]   # 进 AI 的 reps
gaps, bio = dedup_secondary(news_titles, gaps, bio)
```

### 2.4 配置 + 测试

```python
FINAL_DEDUP_ENABLED = True
FINAL_DEDUP_THRESHOLD = 0.84     # 比去重稍松：跨板块撞题判定
```

测试（并入 `test_finalcheck.py`）：embed None 透传；mock 高相似 → gap/bio 被剔除；低相似 → 全保留；空 news_titles 透传。

> 与 #1 同一 PR：两者都只是 `embed_titles` 的复用，边际成本极低。

---

## 3. 参考源深度天花板（独立）

### 3.1 现状与缺口

[scoring.py:196](../digest/scoring.py) 代表作优先挑非 reference 源，但整簇全是 reference（Reuters/AP/Bloomberg 走 Google News 代理）时只能用浅的。`attach_fulltexts`（[main.py:277](../main.py)）对这些源抓不到正文 → 永远 📡 快讯浅评。

### 3.2 方案：高分 reference 条目反查同题全文源回填

复用 scout 的两件工具：[scout.py:43 `_tool_search`](../digest/scout.py)（Exa→Tavily，`search_provider.web_search`）+ `fetch.fetch_one_fulltext`。

新增 `digest/backfill.py`：

```python
PREFERRED_FULLTEXT_DOMAINS = ("bbc.co", "theguardian.com", "aljazeera.com",
                              "cnbc.com", "dw.com", "nikkei.com")

def backfill_reference_depth(articles: list[dict]) -> None:
    """对『高分 + reference 源 + fulltext 空』的条目，搜同题全文源回填正文。
    任何失败 → 跳过该条，绝不中断主流程。"""
    targets = [a for a in articles
               if a.get("reference") and not a.get("fulltext")
               and a.get("score", 0) >= BACKFILL_MIN_SCORE][:BACKFILL_MAX]
    for a in targets:
        try:
            results = web_search(a["title"], num=5)
            for r in results:
                if any(d in r["url"] for d in PREFERRED_FULLTEXT_DOMAINS):
                    text = fetch_one_fulltext({"link": r["url"], "title": "",
                                               "source": "backfill"})
                    if text:
                        a["fulltext"] = text[:FULLTEXT_MAX_CHARS]
                        a["backfill_source"] = r["url"]    # 来源诚实标注
                        log.info(f"回填正文成功：{a['title']} ← {r['url']}")
                        break
        except Exception as e:
            log.warning(f"回填失败（跳过）：{a.get('title')} — {e}")
```

调用点：`main.py` 在 `attach_fulltexts(articles)`（[main.py:277](../main.py)）**之后**。

### 3.3 关键风险：跨源来源真实性（红线）

回填的全文来自**另一家媒体**。来源必须诚实——`_articles_to_text` 注入时标明：

```
正文据 {backfill_source} 同题报道；原报道来源 {原 source}（{原 link}）
```

否则张冠李戴 = 幻觉。这条比 #1/#2 危险，prompt 与来源标注要单独测扎实，**独立 PR**。

### 3.4 配置 + 预算

```python
BACKFILL_ENABLED = True
BACKFILL_MIN_SCORE = 8.0    # 仅给重要的 reference 条目花搜索预算
BACKFILL_MAX = 3            # 每期最多回填几条（每条 1 搜 1 抓，护栏）
```

预算上限是必须的：每条要 1 次 search + 1 次 read，不限量会拖慢主流程并烧 Exa 月额度（`SEARCH_MONTHLY_CAP`）。

---

## 4. 每条信息密度地板

### 4.1 现状与缺口

`score_importance` 有实体密度维度（[scoring.py:86](../digest/scoring.py)，`ENTITY_DENSITY`），但那是**选稿前**对标题打的；**成稿点评**的信息密度无人评判。读者偶尔收到「点评写空话、三段塌成一段」的条目。

### 4.2 两种落地

**A. 纯代码版（不依赖特性二，可独立先上）**：成稿后逐条算硬指标，纯函数、零 API。

```python
# digest/finalcheck.py
def density_floor(items: list[dict]) -> list[str]:
    """返回未达地板的条目标题清单。复用 _HARD_NUMBER_RE + extract_proper_nouns。"""
    bad = []
    for it in items:
        body = it["body"]      # 三段正文（剥掉标题行与 📰 来源行）
        facts = len(_HARD_NUMBER_RE.findall(body)) + len(extract_proper_nouns(body))
        if len(body) < DENSITY_MIN_CHARS or facts < DENSITY_MIN_FACTS:
            bad.append(it["title"])
    return bad
```

先只**记日志**观察哪些条目常踩线，再调阈值——和关键词表一样属「主编口味」。

**B. 接特性二**：把 `density_floor` 输出 append 进特性二 `evaluate_digest` 返回的 `issues`，自然触发那几条的定向重写（与 `DESIGN-semantic-dedup-and-self-critique.md` §2.3/§3.4 衔接）。

### 4.3 成稿切条解析器（#2 与 #4 共用）

A 版与 #2 都需要把整块 markdown 成稿切成「单条」。在 `finalcheck.py` 写一个共用解析器：

```python
def split_items(summary: str) -> list[dict]:
    """按 **标题行** … > 📰 来源 切成条目。
    返回 [{title, body, source_line}]。解析失败 → 返回 []（调用方据此跳过）。"""
```

> 注：若 #2 走 §2.3 推荐的「用 reps 当基准」路线，#2 本身不需要 `split_items`；但 #4-A 评估成稿点评质量绕不开切条，仍需此解析器。

### 4.4 配置 + 测试

```python
DENSITY_FLOOR_ENABLED = True
DENSITY_MIN_CHARS = 120     # 三段正文最少字数
DENSITY_MIN_FACTS = 2       # 数字 + 专有名词最少事实点
```

测试（`test_finalcheck.py`）：`split_items` 正常切分 / 畸形输入返回 []；`density_floor` 命中低密度条目；高密度条目不进清单。

---

## 5. 落地顺序与开放问题

### 建议顺序

0. **先补特性一嵌入基建**（`digest/embedding.py::embed_titles` + `scoring.py::merge_similar_clusters` + `requirements.txt` 加 `fastembed` + config 语义键），让现成的 `test_semantic_dedup.py` 转绿。这是 1/2/4 的地基，本身也是「语义去重」特性本体。
1. **PR-A**：#1 跨期串联 + #2 成稿终检（同 PR，共吃 `embed_titles`，+ sidecar 存档改动）。
2. **PR-B**：#4-A 纯代码密度地板（先只记日志摸阈值）。特性二上线后再做 #4-B 并入 issues。
3. **PR-C**：#3 参考源回填（独立，碰跨源来源真实性，单独测）。

### 开放问题（需主编口味拍板）

1. **`PROGRESS_SIM_THRESHOLD`**（进展判定）：0.80 起步？更怕漏就降到 0.76，更怕误标升到 0.84。
2. **`FINAL_DEDUP_THRESHOLD`**（跨板块撞题）：0.84 起步是否合适？
3. **#3 回填的目标域名白名单**（`PREFERRED_FULLTEXT_DOMAINS`）：除 BBC/Guardian/AlJazeera 外还想加哪些能抓全文的同题源？
4. **`BACKFILL_MAX`**：每期回填上限 3 条够不够？预算 vs 深度的取舍。
5. **密度地板阈值**（`DENSITY_MIN_CHARS` / `DENSITY_MIN_FACTS`）：120 字 / 2 事实点是否过严？建议先记日志看分布再定。
6. **sidecar 落地方式**：写稿阶段 dump（推荐）还是改 `save_digest_markdown` 签名带上 reps？
