# 设计文档：语义去重 + 自评重写环

> 状态：草案 · 待评审
> 范围：`digest/` 内两个内容质量特性
> 约束：**只有 DeepSeek API**（纯文本对话补全；无 embedding / 无视觉 / 无 TTS；单一供应商）

---

## 0. 背景与目标

当前管线（`main.py`）：

```
fetch_all_feeds  →  triage_with_deepseek  →  scout/bio/github  →  attach_fulltexts  →  summarize_with_deepseek  →  插板块  →  推送
  打分+词面聚类         R1 轻量精选≤13          独立板块            只给选中条抓正文        成文(分批/合并)
  +分桶候选~25条
```

两个问题：

1. **去重是词面的**。`scoring.same_story` 靠标题专有名词 + 关键词重叠判断同一事件。同一件事换一种说法（不同媒体重写标题）就合并不了，晨报出现重复事件。
2. **没有质量度量**。`factcheck` 只防幻觉（数字冲突、未来日期、来源缺失），但「这篇点评是不是有信息增量、有没有空话、三段式到没到位」无人评判。

目标：在**不引入第二个 LLM 供应商**的前提下，
- 特性一：用**本地嵌入向量**补 DeepSeek 唯一硬缺口（无 embedding 接口），做语义级去重；
- 特性二：用 **DeepSeek 自己当裁判**（LLM-as-judge），对成稿打分并按需重写一次。

---

## 1. 特性一：语义去重

### 1.1 现状与缺口

- 聚类发生在 `scoring.cluster_and_boost`：对全部抓取到的文章（聚类前可能 100~300 条）两两跑 `same_story`，合并成簇，每簇选代表作，按「被几家报道」加分。
- `same_story` 是纯词面：`title_keywords`（去虚词的词集合）+ `extract_proper_nouns`（专有名词）。
- 失败案例：
  - "Fed holds rates steady" vs "Powell keeps interest rates unchanged" —— 共享词少、专有名词不重合（Fed vs Powell），判为两条 → 重复。
  - "OpenAI launches GPT-5" vs "GPT-5 is here: what you need to know" —— 后者标题党句式被惩罚，但仍可能各自成簇。

### 1.2 方案总览：在词面聚类之上叠加「向量精筛」

**不替换、只增强**。保留现有词面聚类（快、零成本、已有测试覆盖），在其后加一层语义合并：

```
cluster_and_boost(articles)              # ① 现有：词面聚类 → 簇代表作列表 reps
        │
        ▼
merge_similar_clusters(reps)             # ② 新增：对簇代表作做向量相似度二次合并
        │
        ▼
enforce_category_balance(...)            # ③ 现有：分桶候选
```

为什么放在 `cluster_and_boost` **之后**而不是替换它：
- 词面聚类先把"显然同事件"的合并掉，向量层只需处理剩下的 N 个簇代表作（数量级从「全部文章两两比」降到「几十个代表作两两比」），**计算量小**。
- 词面层已有单测（`test_core.py` 的 `same_story`/`cluster` 用例）不动，回归风险低。
- 向量合并时，沿用 `cluster_and_boost` 已有的「代表作挑选 + 多源加分」逻辑，把两个被判定为同事件的簇合并后重算 `cluster_size` 与 `score`。

### 1.3 依赖选型（关键决策）

| 方案 | 体积 | CI 装依赖耗时 | 质量 | 评价 |
|------|------|--------------|------|------|
| `sentence-transformers` + torch | ~1–2 GB | 数分钟 | 高 | ❌ 对免费 Actions 过重 |
| **`fastembed`（onnxruntime）** | ~50 MB + 模型 ~100 MB | 数十秒 | 高 | ✅ **推荐**：无 torch，量化 ONNX 模型 |
| `model2vec`（静态嵌入） | ~10 MB | 秒级 | 中 | 备选：极轻但短标题质量略降 |

**推荐 `fastembed` + `intfloat/multilingual-e5-small`**：
- 多语言（注意 `36kr` 是中文源，标题中英混合，必须多语言模型）。
- ONNX 量化，CPU 上 embed 300 条短标题 < 1 秒。
- 模型首次会下载到缓存目录；用 `actions/cache` 缓存该目录，后续 run 免下载（见 §1.6）。

> 备选退路：若不想引入 onnxruntime，用 `model2vec` 的 `potion-multilingual-128M` 静态模型，纯 numpy 推理、装包秒级，代价是短标题区分度略低、阈值要调松。

### 1.4 算法

```python
# digest/embedding.py（新文件）
def embed_titles(titles: list[str]) -> "np.ndarray | None":
    """把标题列表编码成 L2 归一化向量矩阵。
    模型加载失败 / 库未安装 → 返回 None（调用方据此整体跳过语义层，回退纯词面）。"""

# digest/scoring.py（新增）
def merge_similar_clusters(reps: list[dict], threshold: float) -> list[dict]:
    """对词面聚类后的簇代表作做向量二次合并。
    1. 取每个 rep 的 title(+summary 前 N 字) 编码；embed 返回 None 直接原样返回 reps。
    2. 两两余弦相似度 ≥ threshold 视为同事件，用并查集合并。
    3. 合并时复用 cluster_and_boost 的代表作规则：
       - 优先非 reference 源、原始 score 最高者当代表；
       - cluster_size 累加、cluster_titles 合并、score 重算多源加成。
    4. 返回合并后的代表作列表（重新按 score 降序）。"""
```

要点：
- **向量做 L2 归一化后，余弦相似度 = 点积**，一次矩阵乘法 `M @ M.T` 得到相似度矩阵，无需循环。
- 合并用**并查集（union-find）**，避免"A~B、B~C 但 A 不直接相似"时漏并。
- 编码文本用 `title`，可选拼 `summary[:120]` 提升短标题区分度（实测调）。
- e5 系列要求查询/文档加前缀（`"query: "` / `"passage: "`）；这里全部当对称文本，统一加 `"passage: "` 即可。

### 1.5 配置（`config.py` 新增）

```python
SEMANTIC_DEDUP_ENABLED = True          # 总开关，库缺失/出错自动降级为 False 效果
EMBED_MODEL = "intfloat/multilingual-e5-small"
SEMANTIC_SIM_THRESHOLD = 0.86          # 余弦 ≥ 此值判同事件（⚠️ 需调，见开放问题）
SEMANTIC_USE_SUMMARY = True            # 编码是否拼接 summary 前若干字
SEMANTIC_SUMMARY_CHARS = 120
```

### 1.6 CI 影响

`requirements.txt` 增 `fastembed`（连带 onnxruntime、numpy）。`.github/workflows/daily-digest.yml` 加一步模型缓存：

```yaml
- name: 🧠 缓存嵌入模型
  uses: actions/cache@v4
  with:
    path: ~/.cache/fastembed     # fastembed 默认模型缓存目录
    key: fastembed-mle5-small-v1
```

> 影响评估：首次 run 多下载 ~100MB 模型（数十秒），之后命中缓存近乎零成本。装包时间从「秒级」升到「~30–60 秒」，对一天两次的 job 可接受。

### 1.7 测试

- `test_core.py` 既有词面用例**不动**（保证回归）。
- 新增 `test_semantic_dedup.py`：
  - `embed_titles` 库缺失时返回 `None`（mock import 失败）；
  - `merge_similar_clusters` 在 `embed` 返回 `None` 时**原样透传**（核心降级保证）；
  - 喂入手造的高相似度向量（直接 mock 编码结果），断言被合并、`cluster_size` 正确累加、代表作选择符合 reference 优先规则；
  - 低相似度向量断言不合并。
- **关键：mock 掉真实模型**，单测不下载、不依赖网络，CI 的 `pytest test_core.py` 门禁保持快。

### 1.8 风险与回退

| 风险 | 缓解 |
|------|------|
| 库装不上 / 模型下载失败 | `embed_titles` 返回 `None` → 整层跳过，回退纯词面聚类，**绝不中断推送** |
| 阈值过松 → 误并不同事件 | 阈值可配；并且向量层只在词面层之后，错误面有限；上线先观察日志 |
| 阈值过严 → 没起作用 | 日志打印「向量层额外合并了 N 簇」便于调参 |
| CI 变慢 | 模型缓存 + 单测 mock，不在门禁路径真跑模型 |

### 1.9 协同红利

同一套 `embed_titles` 还能复用到：
- **跨期事件串联**（把今天的簇代表作向量与 `digests/` 近几期标题向量比，自动标 📈进展，比现在纯靠 LLM 判断更稳）；
- **成稿内近重复终检**（summarize 后再扫一遍最终条目，防漏网重复）。

---

## 2. 特性二：自评重写环（self-critique loop）

### 2.1 现状与缺口

`ai.summarize_with_deepseek` 出稿后只过 `factcheck.sanity_check_output`（事后幻觉扫描，仅记日志、不修改）。**没有质量打分、没有重写**。读者偶尔会收到「点评写空话、三段式塌成一段、个人雷达没落地」的条目。

### 2.2 方案：judge → （按需）rewrite ≤ 1 次

```
summary = summarize_with_deepseek(articles)
        │
        ▼
report = evaluate_digest(summary)          # 新增：DeepSeek 当裁判，输出结构化评分+问题清单
        │
        ├─ score ≥ REWRITE_THRESHOLD ──────────────────► 用原稿
        │
        └─ score < REWRITE_THRESHOLD 且 预算未用尽
                 │
                 ▼
            summary2 = revise_digest(summary, report.issues)   # 新增：带着问题清单重写一次
                 │
                 ▼
            sanity_check_output(summary2)   # 必须复扫
                 │
                 ├─ 重写后更差/格式破坏 ──► 回退原稿
                 └─ 否则 ──► 用 summary2
```

插入点：`main.py` 中 `summary = summarize_with_deepseek(articles)`（约 280 行）之后、各 `_insert_*` 板块拼接**之前**。封装成 `digest/critique.py`，`main.py` 只加一行 `summary = refine_digest(summary)`。

### 2.3 裁判契约（结构化 JSON，复用 triage 的鲁棒解析思路）

```python
# digest/critique.py
def evaluate_digest(summary: str) -> dict:
    """让 DeepSeek 给成稿按维度打分。返回 {"overall": 1-10, "issues": [str, ...]}。
    解析失败 → 返回 {"overall": 10, "issues": []}（视为通过，绝不阻断推送）。"""
```

裁判 system prompt 评估维度（**权重/阈值是编辑口味，见开放问题**）：
1. 信息密度：是否有「空话/正确的废话」，有没有具体事实/数据/因果；
2. 三段式：【核心事实】【深层逻辑】【后市/影响】是否齐全且各司其职；
3. 本土化与个人雷达：对中国/读者画像的影响是否落到实处（不是泛泛「利好行业」）；
4. 重复：是否有两条在讲同一事件；
5. 来源：每条是否带 `📰 来源`。

输出强制纯 JSON（学 `triage` 的 `_extract_decisions` 抗截断/夹带解析）。

### 2.4 重写契约（**格式保全是最大风险**）

```python
def revise_digest(summary: str, issues: list[str]) -> str:
    """带着问题清单重写。强约束：保持原 Markdown 结构与每条的 📰 来源行原样，
    只改进点评文字；不得新增素材里没有的数字/人名/引语。"""
```

重写 prompt 必须钉死：
- **逐条保留来源行**（`> 📰 来源：…`）一字不改 —— 这是最容易被重写弄丢的；
- 不准引入新事实（重写不是再创作）；
- 保持三大板块标题与条目数量。

> ⚠️ 重写让整稿重过一遍模型，可能丢链接、动结构、甚至引新幻觉。所以**重写后必须复跑 `sanity_check_output`**，并做一个「来源条数不降、长度不暴跌」的硬校验，任一不满足就**丢弃重写、用原稿**。宁可不改，不可改坏。

### 2.5 配置（`config.py` 新增）

```python
SELF_CRITIQUE_ENABLED = True
CRITIQUE_MODEL = "deepseek-v4-pro"     # 裁判吃判断力，用 pro 思考模式
REWRITE_THRESHOLD = 7.0                # overall < 此分触发重写（⚠️ 调，见开放问题）
REWRITE_MAX = 1                        # 最多重写次数（防烧钱/死循环）
```

### 2.6 成本与观测

- 正常路径：**+1 次裁判调用**（输入是成稿，输出短 JSON，便宜）。
- 触发重写：再 +1 次重写调用。重写应是少数情况。
- 记录每期 `overall` 分到日志（后续可进 §3 的成本/质量台账），观察趋势、调阈值。

### 2.7 测试

`test_critique.py`：
- `evaluate_digest` 解析失败 → 返回通过（`overall=10`）；
- `refine_digest` 在 judge 高分时**不调用重写**（mock 计数）；
- 低分时调一次重写；
- 重写后来源条数下降 → 回退原稿（核心安全用例）；
- `REWRITE_MAX` 生效，不会重写第二次。
- 所有 DeepSeek 调用 mock，门禁不真打 API。

### 2.8 风险与回退

| 风险 | 缓解 |
|------|------|
| 裁判误判（好稿打低分）触发无谓重写 | 阈值保守起步（如 6.0）；重写有 `REWRITE_MAX=1` 上限 |
| 重写丢链接/破格式/引新幻觉 | 重写后复跑 `sanity_check_output` + 来源条数/长度硬校验，不达标弃用重写 |
| 裁判/重写调用失败 | 任一异常 → 用原稿，绝不阻断推送（与 triage/scout 同一降级哲学）|
| 额外 token 成本 | 裁判输入是已成稿、输出短；重写是少数路径；可配关 |

---

## 3. 内容质量还能往哪提升（更广的清单）

按「投入产出 / 与上面两特性的协同」排序：

1. **跨期事件串联做实**（高协同）。现在 📈进展 全靠 LLM 看 `recent_digests` 文本判断，易漏易误。用特性一的 `embed_titles` 把今天的条目与近几期标题做向量匹配，命中即结构化标注「这是 X 事件的进展」，喂给写作 prompt。**复用同一套嵌入，几乎零边际成本。**
2. **成稿内近重复终检**（高协同）。聚类发生在抓取阶段，但 scout/bio/github 板块的内容可能与主新闻撞题。出稿后用向量再扫一遍最终条目，发现近重复就提示或合并。
3. **参考源的深度天花板**。Reuters/AP/Bloomberg 走 Google News 代理、抓不到正文 → 永远是 📡快讯浅评。可让 scout 对「重要但仅参考源」的事件，反查一个能抓全文的同题源（BBC/Guardian 常有），把深度补回来。独立于上面两特性，工作量中等。
4. **每条「信息密度地板」**。给单条点评设最小实质字数/事实点阈值，达不到的条目直接进重写清单（可并入特性二的 issues）。
5. **裁判维度沉淀为可调权重**。把 §2.3 的评估维度抽成配置化的 rubric，让你像调关键词表一样调"什么算好稿"，逐步把你的编辑口味固化下来。
6. **反馈闭环**（更大工程，已在上一轮路线图）。Telegram 👍/👎 → 调权重，使 readerprofile 从手调走向数据驱动。不在本设计范围，但裁判分 + 反馈数据将来可互相印证。

> DeepSeek 做不到、且不值得为它引外部依赖的：配图/图表、语音播报。本设计范围内不碰。

---

## 4. 落地顺序与开放问题

**建议顺序**：先特性一（读者每天直接可感的「重复新闻」问题，且零 API 成本、为 §3.1/3.2 铺路）→ 再特性二。两者互不耦合，可分两个 PR。

**需要你（主编口味）拍板的开放问题**：

1. **去重相似度阈值**（`SEMANTIC_SIM_THRESHOLD`）：0.86 偏稳；要更激进合并降到 0.82，要更保守升到 0.90。建议先 0.86 上线看日志再调。
2. **裁判评分维度与权重**（§2.3）：五个维度是否都要？哪个最该重罚（我猜是"空话/信息密度"）？
3. **重写触发阈值**（`REWRITE_THRESHOLD`）：保守 6.0（少重写、省钱）还是激进 7.5（多重写、追质量、费 token）？
4. **重写粒度**：整稿重写（简单，本设计默认）还是只挑被点名的低分条目逐条重写（更精准但实现复杂、要能定位单条）？
5. **嵌入依赖**：接受 `fastembed`（onnxruntime，~150MB，质量好）还是要更极致轻量的 `model2vec`（~10MB，质量略降）？
