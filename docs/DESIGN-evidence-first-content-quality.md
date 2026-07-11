# P2 证据优先的内容质量增强设计

> 状态：Proposed（待实施）  
> 日期：2026-07-11  
> 范围：`daily-news-digest` 主新闻链路  
> 目标版本：P2-A 至 P2-E，分阶段上线

## 1. 背景

当前项目已经具备较完整的内容质量链路：RSS 粗筛、DeepSeek triage、正文抓取、参考源同题回填、跨期事件关联、语义去重、自评重写、信息密度观察、幻觉扫描和多渠道推送。

当前主要缺口不再是“有没有质量检查”，而是质量检查缺少统一、结构化的证据输入：

- AI 直接读取标题、摘要和正文，事实、推断、未知项没有显式分层；
- `backfill_source` 能标记同题回填来源，但没有统一的来源角色和证据等级；
- `density_floor` 只能从成稿文本反推信息密度，无法判断事实是否真的来自输入材料；
- 跨期 sidecar 保存标题和事件线索，但未保存本期采用的事实锚点；
- 质量指标主要写日志，缺少可长期比较的结构化运行报告。

因此，本轮优化采用“证据优先”原则：先把每条新闻的事实依据整理为可复核的证据卡片，再让 AI 写作；写完后按条目检查成稿是否仍受这些证据约束。

## 2. 目标与非目标

### 2.1 目标

1. 每条主新闻在写作前都生成结构化证据卡片。
2. 明确区分原始来源、补充全文来源和多源佐证来源。
3. 明确区分已确认事实、分析性推断和材料未说明的信息。
4. 让成稿中的每条事实能够回到当次抓取材料的受限原句、内容哈希和来源，而不是只检查卡片内部自洽。
5. 保存每期证据和质量指标，支持阈值调优、问题复盘和跨期事件串联。
6. 所有新增能力均可降级，失败不得阻断日报推送。

### 2.2 非目标

- 不替换现有 RSS、规则评分、triage、语义去重或自评重写链路。
- 不新增第二个 LLM 供应商。
- 不做开放互联网事实核查器；搜索只用于现有参考源回填场景。
- 不要求每条新闻包含数字；事实锚点可以是日期、人物、机构、地点或明确动作。
- 不在第一阶段自动重写所有低密度条目。
- 不把推送正文变成长篇研究报告。

## 3. 设计原则

### 3.1 来源诚实

原始报道和同题补充报道必须分开记录。来自 B 媒体的正文不得被描述成 A 媒体的原文。

### 3.2 证据约束写作

AI 可以解释事实的意义，但不能把输入材料未提供的数字、人名、引语和因果关系写成已确认事实。

### 3.3 先观察，再自动干预

证据覆盖率和信息密度先进入结构化报告。积累至少 14 天数据后，再决定自动重写阈值。

### 3.4 失败开放

证据抽取、回填、指标保存或一致性检查失败时，记录原因并回退到当前稳定链路。质量增强不能导致整期漏推。

### 3.5 成本有上限

纯规则能完成的工作不调用 LLM。证据抽取优先使用现有字段和正则；只有语义归纳需要时才复用当前 DeepSeek 调用。

## 4. 当前链路与目标链路

### 4.1 当前链路

```text
fetch_all_feeds
  -> triage_with_deepseek
  -> attach_fulltexts
  -> backfill_reference_depth
  -> tag_progress
  -> summarize_with_deepseek
  -> refine_digest
  -> density_floor
  -> 插入其他板块并推送
```

### 4.2 目标链路

```text
fetch_all_feeds
  -> triage_with_deepseek
  -> attach_fulltexts
  -> backfill_reference_depth
  -> build_evidence_cards             [新增]
  -> tag_progress
  -> summarize_with_deepseek          [改为读取 evidence_card]
  -> refine_digest
  -> density_floor                    [范围：主新闻]
  -> 插入 scout/bio/github/signals/topics
  -> strip_audit_block
  -> validate_main_digest_evidence    [新增；对最终 Markdown 中的主新闻逐条校验]
  -> build_main_quality_report        [新增；明确排除其他板块]
  -> strip_internal_article_ids       [新增；推送前移除内部标记]
  -> 推送
  -> save_evidence_sidecar            [新增；仅成功送达后]
  -> save_quality_report              [新增；仅成功送达后]
  -> prune_quality_artifacts          [新增；成功送达并保存后]
```

本轮质量报告的正式名称为“主新闻质量报告”。Scout、生物、GitHub、信号和自媒体选题使用不同的数据结构，本轮不宣称覆盖它们。校验发生在最终 Markdown 组装完成之后，但解析器只读取带 `article_id` 的主新闻条目；其他板块不进入分母，也不会用它们的数字支持主新闻。

## 5. 核心数据模型

### 5.1 EvidenceCard

每条 triage 入选新闻新增 `evidence_card` 字段：

```python
EvidenceCard = {
    "version": 1,
    "id_scheme_version": 1,
    "article_id": "a1_<sha256(identity_key)[:24]>",
    "headline": "原始标题",
    "confirmed_facts": [
        {
            "fact_id": "f1",
            "text": "可直接陈述的事实",
            "source_id": "source:primary",
            "captured_at": "2026-07-11T08:04:31+08:00",
            "content_sha256": "当次抓取材料规范化后的 SHA-256",
            "excerpt": "从当次材料截取的原句，最多 280 个 Unicode 字符",
            "sentence_hash": "规范化 excerpt 的 SHA-256",
            "anchors": ["OpenAI", "2026-07-11"]
        }
    ],
    "entities": ["人物、机构、地点"],
    "numbers": ["金额、比例、数量"],
    "dates": ["明确日期或时间范围"],
    "unknowns": ["材料没有说明、写作时不得补齐的事项"],
    "sources": [
        {
            "id": "source:primary",
            "role": "primary",
            "publisher": "Reuters",
            "url": "原始 RSS/报道链接",
            "canonical_url": None,
            "hostname": "reuters.com",
            "content_level": "summary",
            "trust_tier": "major_media",
            "captured_at": "2026-07-11T08:04:31+08:00",
            "content_sha256": "当次用于抽取的标题/摘要/正文哈希"
        },
        {
            "id": "source:backfill",
            "role": "context",
            "publisher": "BBC",
            "url": "同题全文链接",
            "canonical_url": "https://www.bbc.com/news/articles/example",
            "hostname": "bbc.com",
            "content_level": "fulltext",
            "trust_tier": "major_media",
            "captured_at": "2026-07-11T08:04:38+08:00",
            "content_sha256": "同题全文规范化后的 SHA-256"
        }
    ],
    "coverage": {
        "has_fulltext": True,
        "has_multiple_sources": True,
        "fact_count": 4,
        "anchor_count": 6
    }
}
```

### 5.2 字段语义

`role` 只允许：

- `primary`：本条新闻原始来源；
- `context`：同题全文或背景补充来源；
- `corroboration`：同一事件的其他媒体标题或摘要，只用于佐证。

`content_level` 只允许：

- `fulltext`：正文抓取成功；
- `summary`：只有 RSS 摘要或导语；
- `headline`：只有标题。

`trust_tier` 第一版只做粗粒度标记：

- `official`：政府、监管机构、公司官方发布；
- `major_media`：配置中认可的主流媒体；
- `secondary`：搜索得到的其他可接受媒体；
- `unknown`：无法判定，不可作为新增关键事实的唯一依据。

### 5.3 保存受限证据片段，而非保存全文

每个 `confirmed_fact` 必须保存一个可人工审阅的 `excerpt`，不能只保存来源 ID 和 URL。`excerpt` 是从当次实际用于写作的标题、摘要或正文中截取的原句，最多 280 个 Unicode 字符；同时保存 `content_sha256` 和 `sentence_hash`。

第一版不保存整篇原文，也不依赖网页未来仍可访问。原因：

- 当前输入来源包含 RSS 摘要、网页正文和同题回填，文本结构不稳定；
- 保存大段原文会放大仓库体积和版权风险；
- 受限原句使事实可人工审阅，并配合哈希防止保存后被意外改写；
- P2-A 不建设全文检索数据库。

当 `excerpt` 因异常无法生成时，该事实不得进入 `confirmed_facts`。只有标题的条目可以生成最小卡片，但标题本身必须作为 excerpt 保存，并将 `content_level=headline`。字符偏移不作为第一版契约，因为 HTML 清洗会改变偏移；`sentence_hash` 用于检测片段是否被意外改写。由于不保存完整原始材料，本方案不宣称能以密码学方式独立证明 excerpt 必然是原文子串。

### 5.4 `article_id` 生成规则

`article_id` 必须由统一函数生成，禁止各模块自行拼接：

```python
def build_article_id(article: dict) -> str:
    """按 id_scheme_version=1 生成稳定 ID。"""
```

版本 1 的 `identity_key` 优先级：

1. 成功提取 canonical URL：`url:<canonical_url>`；
2. 普通非 Google News URL：`url:<normalized_url>`；
3. Google News 代理、空链接或无法稳定规范化：`fallback:<normalized_source>|<normalized_title>|<published_utc_or_logical_date>`。

URL 规范化算法固定为：scheme/hostname 小写；移除 fragment；移除默认端口；路径合并重复 `/` 并移除非根路径尾部 `/`；query 参数排序；删除 `utm_*`、`fbclid`、`gclid`、`oc` 等跟踪参数。Google News `/rss/articles/...` 代理 URL 不作为稳定 canonical URL，除非成功解析到目标站 URL。

无发布时间时使用本次班次的 logical date，而不是抓取时分秒，避免同一班次重跑生成不同 ID。保留 `id_scheme_version`，以后修改算法时可并行读取旧 ID，不静默重算历史数据。

### 5.5 成稿条目与 `article_id` 绑定

AI 生成主新闻 Markdown 时，每条标题前必须输出内部注释：

```markdown
<!-- article_id:a1_0123456789abcdef01234567 -->
**🔥 中文标题**
```

`refine_digest` 必须把这些注释视为保全字段：条目 ID 数量、集合或顺序变化时，重写稿无效并回退原稿。最终组装后，`parse_main_items()` 仅解析带合法 ID 的主新闻条目，得到：

```python
[{"article_id": "a1_...", "title": "...", "body": "...", "source_line": "..."}]
```

校验完成后、推送之前调用 `strip_internal_article_ids()` 删除注释。缺失、重复或未知 ID 都产生 `unbound_item` 告警；不允许使用“整篇成稿任意位置出现过该数字”作为支持证据。

### 5.6 预期条目与最终渲染条目

在调用 `summarize_with_deepseek()` 之前固定：

```python
expected_article_ids = [article["article_id"] for article in articles]
```

最终 Markdown 组装并解析后计算：

```python
rendered_article_ids = [item["article_id"] for item in parsed_main_items]
missing_article_ids = expected_set - rendered_set
unexpected_article_ids = rendered_set - expected_set
duplicate_article_ids = ids_with_count_gt_1(rendered_article_ids)
```

契约如下：

- `expected_article_ids` 是主新闻条目数、卡片有效率和渲染完整率的权威分母；
- `rendered_article_ids` 只统计最终 Markdown 中成功绑定的主新闻，不含其他板块；
- 每个 `missing_article_id` 产生 `code=unbound_item, reason=missing_rendered_item`；
- unexpected、duplicate、malformed ID 同样产生 `unbound_item`，但不增加预期条目分母；
- 模型省略一条不会影响该条卡片是否有效：卡片有效率仍按 expected 集合计算；
- 另设渲染完整率 `|unique(rendered ∩ expected)| / |expected|`，专门衡量模型遗漏；
- `expected_article_ids` 为空时该 run 不进入主新闻内容质量样本，记录 `no_expected_main_items`，避免除零。

## 6. 模块设计

### 6.1 新增 `digest/evidence.py`

公开接口：

```python
def build_evidence_card(article: dict) -> dict:
    """从单条 article 构造 EvidenceCard；失败时返回最小可用卡片。"""

def build_evidence_cards(articles: list[dict]) -> None:
    """原地为每条 article 写入 evidence_card；单条失败不影响其他条目。"""

def validate_evidence_card(card: dict) -> list[str]:
    """返回结构问题列表；空列表表示结构有效。"""
```

第一版采用纯代码抽取：

- `entities`：复用现有专有名词提取逻辑；
- 
umbers`：复用 `factcheck` 的数字正则；
- `dates`：复用日期扫描规则；
- `sources`：由 `source/link/reference/backfill_source/cluster_titles` 组装；
- `confirmed_facts`：从标题、摘要、正文的前若干句中选取包含实体、数字、日期或明确动作的原句；每条事实绑定 `source_id/captured_at/content_sha256/excerpt/sentence_hash`；
- `unknowns`：第一版只记录系统能确定的缺口，如“仅有标题”“正文未抓到”“缺少明确时间”，不让 LLM自由生成未知项。

第一版不增加额外 API 调用，降低上线风险。`content_sha256` 对“本次实际提供给抽取器的规范化内容”计算，规范化仅统一换行和连续空白，不改写文字。若规则抽取效果不足，再单独设计 P2-A2 的 LLM 结构化抽取。

### 6.2 修改 `digest/ai.py`

`_articles_to_text()` 优先渲染 `evidence_card`：

```text
证据等级：原始来源仅摘要；背景补充来源有全文
已确认事实：...
事实锚点：OpenAI / 2026-07-11 / 30%
材料未说明：具体生效范围
来源角色：
- 原始消息：Reuters <url>
- 背景补充：BBC <url>
```

System prompt 新增硬规则：

1. “核心事实”只能来自 `confirmed_facts` 和对应来源。
2. “深层逻辑”允许推断，但必须使用分析性措辞，不得伪装成来源结论。
3. `unknowns` 中的内容不得自行补齐。
4. 同题补充来源只能作为背景，不得冒充原始来源。
5. 无全文条目必须收缩表述，不得输出精确引语或材料中不存在的细节。

兼容策略：没有 `evidence_card` 时保留当前标题/摘要/正文渲染逻辑。

### 6.3 扩展 `digest/backfill.py`

保留当前 `backfill_source`，新增：

```python
article["backfill"] = {
    "url": url,
    "canonical_url": canonicalize_url(url),
    "hostname": normalized_hostname,
    "publisher": result_publisher,
    "candidate_title": result_title,
    "search_rank": 1,
    "match_reason": "preferred_hostname+search_rank",
    "captured_at": now_iso,
    "content_sha256": sha256(normalized_text),
    "purpose": "context",
    "fetched": True,
}
```

兼容期同时写 `backfill_source` 和 `backfill`。所有下游优先读取新结构，旧字段至少保留一个版本。

回填候选仍必须满足：

- `reference=True`；
- 无原始正文；
- 分数达到 `BACKFILL_MIN_SCORE`；
- 解析 URL 后以规范化 hostname 精确命中白名单：`host == allowed` 或 `host.endswith("." + allowed)`；禁止 URL 字符串子串匹配；
- 记录搜索候选标题、匹配理由、抓取时间和内容哈希；
- 每期不超过 `BACKFILL_MAX`。

### 6.4 新增 `digest/quality.py`

公开接口：

```python
def parse_main_items(final_markdown: str) -> tuple[list[dict], list[dict]]:
    """只解析带 article_id 的主新闻；同时返回绑定错误。"""

def validate_main_digest_evidence(
    final_markdown: str,
    evidence_by_article_id: dict[str, dict],
) -> list[dict]:
    """按 article_id 逐条检查高风险数字、来源和回填声明。"""

def build_main_quality_report(
    articles: list[dict],
    final_markdown: str,
    evidence_issues: list[dict],
    thin_article_ids: list[str],
) -> dict:
    """生成明确只覆盖主新闻的结构化质量报告。"""

def summarize_quality_window(
    delivery_runs: dict,
    quality_dir: str,
    start_date: date,
    end_date: date,
) -> dict:
    """以 delivery_runs 为分母，聚合观察窗覆盖率与质量指标。"""
```

问题对象必须可定位：

```json
{
  "code": "unsupported_number",
  "article_id": "a1_...",
  "title": "中文标题",
  "value": "30%",
  "fact_ids_checked": ["f1", "f2"],
  "severity": "warning"
}
```

第一版一致性检查只处理高确定性项目：

- 主新闻条目中的硬数字是否存在于同一 `article_id` 的卡片，不得跨条目借证据；
- article ID 是否缺失、重复、未知或在重写后改变；
- 每条主新闻的来源行是否仍存在；
- 使用回填正文的条目是否保留原始来源与背景补充来源；
- 无全文条目是否出现疑似直接引语；
- 主新闻条目数量是否异常下降。

#### 数值规范化契约（P2-D 第一版）


`normalize_number(surface: str)` 返回 `{digits, unit, status}`。第一版坚持保守匹配：

1. 统一全角/半角数字、Unicode 减号、小数点和千位分隔符；`2,000` 与 `2000` 可比较。
2. 保留数字字面值，不做算术换算、汇率换算、百分比换算或四舍五入；`30%` 与 `0.3` 不相同。
3. 只做单位别名归一化，不做倍率展开：`$2bn` 与 `2 billion USD` 都规范为 `digits=2, unit=USD_BILLION`，可以匹配；`20 亿美元` 规范为 `digits=20, unit=USD_HUNDRED_MILLION`，第一版不与前两者自动判等。
4. 大小写、单复数和明确别名可统一，例如 `USD/$/美元`、`bn/billion`；单位缺失、复合单位或语义歧义返回 `status=not_comparable`。
5. 只有 `digits` 与规范化 `unit` 均完全相同才判为 supported。一个有单位、一个无单位时不判等。

无法解析的数值产生 `code=number_not_comparable`、`severity=info`，与 `unsupported_number` 分开统计；observe 阶段不据此重写或拦截。禁止为了提高命中率加入隐式倍率、近似值或上下文猜测。

第一版只记录告警，不自动拦截或重写。Scout、生物、GitHub、信号、选题和选稿决策表明确排除，不参与主新闻指标分母。未来要覆盖它们，必须先为每种板块定义独立输入契约和 ID，不能复用主新闻卡片兜底。

### 6.5 扩展 `digest/storage.py`

新增两个按期 sidecar：

```text
digests/meta/YYYY-MM-DD-AM-evidence.json
digests/quality/YYYY-MM-DD-AM.json
```

证据 sidecar：

```json
{
  "version": 1,
  "date": "2026-07-11",
  "session": "AM",
  "items": ["EvidenceCard ..."]
}
```

质量报告：

```json
{
  "version": 1,
  "scope": "main_news_only",
  "run_key": "2026-07-11-AM",
  "date": "2026-07-11",
  "session": "AM",
  "generated_at": "2026-07-11T08:05:02+08:00",
  "expected_article_ids": ["a1_one", "a1_two"],
  "rendered_article_ids": ["a1_one"],
  "missing_article_ids": ["a1_two"],
  "metrics": {
    "expected_item_denominator": 13,
    "rendered_expected_count": 12,
    "render_completeness_rate": 0.923,
    "fulltext_rate": 0.77,
    "multi_source_rate": 0.46,
    "evidence_card_valid_rate": 1.0,
    "thin_item_rate": 0.15,
    "unsupported_number_count": 0,
    "backfill_count": 2
  },
  "issues": []
}
```

### 6.6 sidecar 写入时序与保留策略

证据卡片和质量报告在最终 Markdown 校验完成后先保留于内存；只有至少一个推送渠道成功后才写入磁盘，确保 sidecar 表示“已送达版本”。执行顺序：

1. `save_sent_links()` 保存去重状态，并在 `sent_articles.json.delivery_runs[run_key]` 写入 `evidence_status=pending, quality_status=pending`；
2. `save_digest_markdown()` 和现有 reps sidecar；
3. `save_evidence_sidecar()`；
4. `save_quality_report()`；
5. `mark_artifact_bundle_status(run_key, evidence_status, quality_status)` 更新送达台账；
6. `prune_quality_artifacts(now)`。

各保存函数返回 `bool`，不能只吞异常。任一 sidecar 写入失败不撤销已完成的推送，但必须：

- 输出稳定日志码 `QUALITY_ARTIFACT_WRITE_FAILED`；
- 在 GitHub Actions 中输出 `::warning::` 注解；
- 本期运行摘要分别记录 `evidence_status`、`quality_status` 和派生的 `artifact_bundle_status`。

因此，“成功送达”不等于“工件包必然完整”。任一 sidecar 缺失都是可观测故障，并进入工件包覆盖率的缺失分子。

`delivery_runs` 是工件包覆盖率的权威分母，按 run_key 保存最近 180 天：

```json
{
  "2026-07-11-AM": {
    "delivered_at": "2026-07-11T08:05:10+08:00",
    "evidence_status": "present",
    "quality_status": "present",
    "artifact_bundle_status": "present"
  }
}
```

`artifact_bundle_status` 仅在 `evidence_status=present` 且 `quality_status=present` 时为 `present`；其他组合一律为 `missing`。quality JSON 单独存在不能算作可审计成功，因为没有 evidence sidecar 就无法事后审阅事实片段。

同一 run_key 重试成功时覆盖该键，不增加分母。`save_sent_links()` 与状态更新必须采用临时文件 + 原子替换并返回 `bool`。若送达台账自身写入失败，则输出 `DELIVERY_LEDGER_WRITE_FAILED` Actions warning；该运行无法进入仓库内聚合分母，但可由 Actions 注解审计，属于运行可靠性故障而不是内容质量样本。

保留策略分开配置：

- evidence sidecar：保留 180 天，用于事实复核；
- quality report：保留 90 天，用于阈值观察；
- Markdown 简报和现有 reps sidecar：本轮不改变保留策略。

`prune_quality_artifacts(now)` 是保留策略执行者，在当前 sidecar 保存尝试完成后、进程结束前运行。它只删除文件名和 JSON 内日期均早于 cutoff 的匹配文件；解析失败、路径异常或删除失败时记录 warning 并跳过，绝不扩大删除范围。GitHub Actions 当前已有 `git add digests/`，删除和新增会一起进入提交。

这是机会式保留策略：长期没有成功送达时不会运行清理，实际保留时间可能超过 90/180 天。当前接受该偏差；若未来要求严格 TTL，必须增加独立维护 workflow，不能继续依赖推送成功路径。

需要测试 cutoff 边界、AM/PM 文件、畸形 JSON、文件名与内部日期不一致、删除失败和空目录。不得只声明保留天数而没有清理函数。

## 7. 配置设计

在 `digest/config.py` 新增：

```python
EVIDENCE_CARDS_ENABLED = True
EVIDENCE_MAX_FACTS = 6
EVIDENCE_MIN_ANCHORS = 1
EVIDENCE_TEXT_MAX_CHARS = 3200

EVIDENCE_VALIDATION_ENABLED = True
EVIDENCE_VALIDATION_MODE = "observe"   # observe | enforce
EVIDENCE_REPORT_ENABLED = True

UNSUPPORTED_NUMBER_THRESHOLD = 0       # observe 阶段只记录
EVIDENCE_SIDECAR_RETENTION_DAYS = 180
QUALITY_REPORT_RETENTION_DAYS = 90
```

本设计只实现 `observe`。`enforce` 保留为未来配置值，在误报率达标前不得启用。

## 8. 分阶段实施

### P2-A：证据卡片

改动：

- 新增 `digest/evidence.py`；
- 主流程在 backfill 后生成稳定 `article_id` 并构建卡片；
- 每个 confirmed fact 保存受限 excerpt、抓取时间和内容哈希；
- `_articles_to_text()` 优先读取卡片并输出内部 article ID；
- 增加单元测试。

完成标准：所有主新闻都有稳定 ID 和结构有效的最小卡片；每个 confirmed fact 都可回到当次材料片段；功能关闭或单条抽取失败时仍能正常写稿。

### P2-B：来源透明化

改动：

- `backfill_source` 迁移为结构化 `backfill`，保存规范化 hostname、候选标题、匹配理由、抓取时间和内容哈希；
- 写作输入明确区分原始消息和背景补充；
- 白名单改为解析 hostname 后精确匹配；
- 检查最终成稿是否保留双来源关系。

完成标准：所有使用回填正文的条目都能在输入、日志和成稿来源区分两种来源。

### P2-C：证据 sidecar 与跨期增强

改动：

- 成功送达后保存 evidence sidecar；
- 现有 reps sidecar 增加 `article_id` 和关键事实摘要；
- `progress_of` 可引用此前事实，而不仅是此前中文标题。

完成标准：命中进展事件时，写作输入包含“此前事实 → 当前事实”；sidecar 写入失败不影响推送，但产生稳定 Actions warning 和缺失状态。

### P2-D：质量报告观察期

改动：

- 新增 `digest/quality.py`；
- 固定 expected IDs，并在最终 Markdown 组装后计算 rendered/missing/unexpected/duplicate IDs；
- 按 `article_id` 校验主新闻；
- 把 `density_floor`、证据覆盖、渲染完整率、绑定状态、数值状态和来源完整性汇总为 JSON；
- evidence 与 quality 作为同一工件包记录状态；
- 连续积累至少 14 天。

完成标准：报告分母、覆盖范围和缺失状态均可计算；能够回答主新闻质量是在改善还是退化。

### P2-E：定向重写

前置条件：观察期误报率可接受，且能稳定定位到具体条目。

改动：

- 只将明确有问题的条目加入 `critique` issues；
- 最多重写一次；
- 重写后重新跑证据一致性检查；
- 新增 unsupported 数字或丢失来源时回退原稿。

完成标准：定向重写提高信息密度，同时不增加无证据事实。

## 9. 测试策略

### 9.1 `test_evidence.py`

- 正文完整时生成 `fulltext` 来源和事实锚点；
- 只有摘要时生成最小卡片；
- 只有标题时不抛异常；
- backfill 时正确生成 primary/context 两个来源及规范化 hostname；
- confirmed fact 缺少 excerpt/hash 时卡片无效；
- 普通 URL、跟踪参数、Google News 代理、空链接和无发布时间的 ID 生成符合版本 1 契约；
- 数字、日期、实体去重；
- 非法卡片返回可读问题列表；
- 单条异常不影响批量其他条目。

### 9.2 `test_quality.py`

- 成稿数字存在于同一 article ID 的证据卡片时不告警；
- 数字只存在于其他 article ID 时仍告警；
- expected/rendered 集合完全一致时无绑定告警；
- article ID 缺失、重复、未知或被重写修改时告警；
- missing ID 仍进入卡片有效率分母，并降低渲染完整率；
- 回填条目丢失双来源标记时告警；
- 来源行数量下降时告警；
- 无全文条目出现疑似直接引语时告警；
- `$2bn` 与 `2 billion USD` 可比较，`20 亿美元`、`30%` 与 `0.3` 不做倍率换算；
- 歧义数值产生 `number_not_comparable` 而非 supported；
- Scout/bio/GitHub/signals/topics 的数字不进入主新闻校验；
- evidence 成功但 quality 失败、quality 成功但 evidence 失败时，工件包状态均为 missing；
- evidence/quality 保留期清理的 cutoff 和失败降级正确；
- 报告指标的分子、分母、缺失状态计算正确；
- 空文章、空成稿和畸形 Markdown 均安全降级。

### 9.3 回归测试

必须继续通过：

```powershell
python -m pytest test_core.py test_semantic_dedup.py test_critique.py -q
python -m pytest test_linkage.py test_finalcheck.py test_backfill.py -q
python -m pytest test_evidence.py test_quality.py -q
```

所有网络调用和 DeepSeek 调用必须 mock；CI 单元测试不得依赖真实 API、真实网页或 embedding 模型下载。

### 9.4 Dry-run 验证

建议增加不推送的质量验证入口：

```powershell
python main.py --dry-run --quality-report
```

如果本轮不实现通用 `--dry-run`，至少提供独立脚本读取固定 fixture，输出 evidence/quality JSON，不允许为了验收触发真实微信推送。

## 10. 指标定义与验收标准

### 10.1 统计单位

- `run_key`：`logical_date + session(AM|PM)`；同一 run_key 重试多次，只以最后一次成功送达版本计一次。
- `eligible_run`：`sent_articles.json.delivery_runs` 中存在的 run_key，即至少一个渠道成功送达且送达台账写入成功。抓取失败、测试失败、全部渠道发送失败和 `should_skip_session()` 跳过均不进入内容质量分母。
- `expected_item`：调用写作模型前固定在 `expected_article_ids` 中的唯一 ID。它是卡片有效率与渲染完整率的分母；其他板块不进入。
- `rendered_expected_item`：最终 Markdown 中成功解析、唯一且属于 expected 集合的 ID。
- `valid_card`：expected item 的卡片 schema 通过、ID 合法，且每个 confirmed fact 均包含可人工审阅的 excerpt、source_id、captured_at、content_sha256 和 sentence_hash。模型是否遗漏该条不改变卡片有效性。
- `artifact_bundle_present`：对应 eligible run 的 evidence JSON 与 quality JSON 均存在、可解析，且台账两项状态均为 present。任一缺失均为 false，不从分母排除。

### 10.2 指标公式

- 工件包覆盖率：`count(artifact_bundle_present=True) / count(eligible_runs)`。
- 卡片有效率：`count(valid_cards) / count(expected_items)`；无法解析卡片计无效，模型遗漏不从分母移除。
- 渲染完整率：`count(unique rendered_expected_items) / count(expected_items)`；missing ID 直接降低该指标。
- 双来源标注率：`成稿保留 primary+context 的 backfilled rendered items / all backfilled rendered items`；无回填条目时记为 `not_applicable`，不伪造 100%。
- 告警误报率：`人工判为实际有证据或合法表达的 sampled_unique_issues / sampled_unique_issues`。

`unique_issue` 的去重键为 `(run_key, article_id, code, normalized_value)`；同一问题的重复日志只计一次。人工样本从观察窗全部 unique issues 中用固定随机种子等概率抽取；若不足 30 条则全量检查，不得人工挑选“容易判断”的样本。

### 10.3 观察窗口

观察窗为最近 14 个上海时区自然日，不要求每天固定两个成功班次。窗口内所有 eligible runs 都进入分母；跳过和失败班次另记运行可靠性指标，但不稀释内容质量指标。

### 10.4 P2-D 验收

1. 最近 14 天工件包覆盖率不低于 98%；evidence 或 quality 任一缺失均有 `QUALITY_ARTIFACT_WRITE_FAILED` Actions warning。
2. 卡片有效率不低于 99%。
3. 渲染完整率为 100%；任何模型遗漏均产生带 missing ID 的 `unbound_item`。
4. 跨源回填条目的双来源标注率为 100%。
5. 所有网络调用和 DeepSeek 调用在单元测试中 mock，CI 门禁稳定通过。
6. 证据、校验、保存或清理模块异常不阻断推送。
7. 纯标题条目显式标记 `content_level=headline`，标题作为受限 excerpt 保存。
8. 无证据数字告警包含 run_key、article_id、title、value、规范化结果和检查过的 fact IDs。
9. 保留策略在成功班次中删除超过 cutoff 的匹配 sidecar，且不会删除范围外文件。

### 10.5 P2-E 启用门槛

- 完成至少一个 14 天观察窗；
- 至少人工复核 30 个 unique issues；不足 30 个时延长窗口，不以小样本启用 enforce；
- 总体误报率低于 10%，且 `unsupported_number` 与 `unbound_item` 各自误报率也低于 10%；
- 定向重写回归样本中没有新增无证据事实、丢失来源或 ID 变化。

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 规则抽取把背景句当事实 | 证据卡片质量下降 | 第一版限制事实数量，优先含明确锚点的短句 |
| 成稿数字格式变化导致误报 | 报告噪声 | 第一版只做字符与单位别名规范化；不换算，不可比较项单列 |
| sidecar 增长过快 | 仓库体积增加 | 不保存全文；evidence 保留 180 天，quality 保留 90 天并由清理函数执行 |
| backfill 来源与原事件不完全一致 | 张冠李戴 | 保留域名白名单、只作 context、不得覆盖 primary |
| 质量检查增加运行时间 | 定时任务变慢 | P2-A/P2-D 使用纯代码；限制处理文本长度 |
| 文档与代码再次漂移 | 后续错误实现 | 文档顶部标状态；实现完成后逐阶段更新为 Accepted |

## 12. 关键决策与备选方案

### 决策一：证据卡片先用规则生成

选择原因：零新增 API 成本、结果可测试、失败边界清晰。

暂不选择“每条新闻调用一次 LLM 抽取”：13 条新闻会显著增加调用数、成本和失败点。未来如需 LLM，只允许按批抽取并保留规则降级。

### 决策二：一致性检查先观察，不阻断

选择原因：数字格式、日期换算和合理分析可能产生误报。没有真实分布前直接拦截会降低推送可靠性。

### 决策三：不保存全文

选择原因：控制仓库体积、降低版权和敏感内容风险，并保持当前 collector 的轻量定位。

### 决策四：保持现有 Markdown 推送格式

选择原因：本轮提升输入证据和质量观测，不扩大到客户端展示重构。新增结构化数据只存 sidecar，不破坏微信和 Telegram 阅读体验。

## 13. 建议的 PR 切分

1. `feat/evidence-cards`：P2-A，纯规则证据卡片与 AI 输入。
2. `feat/source-transparency`：P2-B，结构化 backfill 与双来源校验。
3. `feat/evidence-sidecars`：P2-C，sidecar 与跨期事实增强。
4. `feat/quality-observability`：P2-D，质量报告和观察期指标。
5. `feat/evidence-guided-rewrite`：P2-E，达到启用条件后再实施。

每个 PR 必须独立可回滚，并保持“关闭新开关即可回到当前行为”。

## 14. 实施前检查

- 确认当前功能分支已合入生产使用的 `release` 分支；
- 确认现有设计文档中的“未实现”清单不再作为代码事实；
- 固定 10–20 条真实但脱敏的文章 fixture，覆盖全文、摘要、标题、backfill 和跨期进展；
- 先实现 P2-A，不将 P2-E 自动重写混入第一个 PR。

