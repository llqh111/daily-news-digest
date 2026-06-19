"""配置中心：RSS 源、关键词表、信任分、各种常量。

设计原则：
· 本模块只声明常量和"配置即数据"，不做 IO、不做副作用、不依赖其他业务模块。
· 改这里 = 改"主编的口味"，不需要改任何业务逻辑。
· 关键词正则在模块加载时一次性编译（_HIGH_SIGNAL_RE 等），全局复用。
"""

from __future__ import annotations

import os
import re
from datetime import timezone, timedelta

# ═══════════════════════════════════════════════════
#  全局时区 / 路径
# ═══════════════════════════════════════════════════

# 北京时间
TZ = timezone(timedelta(hours=8))

# 已推送记录文件（避免早晚报重复 + 跨天去重）
# 路径基于「项目根目录」而非本文件目录——本文件已挪到 digest/ 子包下。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENT_LOG_FILE = os.path.join(_PROJECT_ROOT, "sent_articles.json")

# 跨天去重保留天数：超过这个天数的旧记录自动清理
SENT_RETENTION_DAYS = 7

# ═══════════════════════════════════════════════════
#  搜索 API 密钥（都可选）
#  Exa 为主用搜索引擎；没配 EXA_API_KEY 则退而用 Tavily；
#  两个都没配时侦察兵静默跳过搜索工具调用。
# ═══════════════════════════════════════════════════

EXA_API_KEY = os.getenv("EXA_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# ═══════════════════════════════════════════════════
#  抓取参数（想改新闻条数、时间窗口、并发，改这里）
# ═══════════════════════════════════════════════════

# 每个 RSS 源最多取几条（抓多一点，留给后面筛选挑）
MAX_PER_FEED = 8
# 只保留多少小时内的新闻（晨报要新鲜，48h 给国际时差留余地）
TIME_WINDOW_HOURS = 24
# 慢源专属窗口：央行/政府/公司博客/newsletter 周更甚至更慢，24h 会天天交白卷。
# 给它们单独放宽到 72h（在源上标 "window_hours" 引用本值即可）。
# 安全性：fetch 里有 skip_links 去重 + sent_articles.json 存 7 天，放宽不会重复推送。
SLOW_WINDOW_HOURS = 72
# 聚类去重 + 打分后，留多少条「代表作」去抓正文全文交给 AI。
# AI 会从这批里再精选，所以候选数要比最终条数大一些，给 AI 留挑选余地。
# 注：候选池不再用「全局 top-N」，改成各分类按 CATEGORY_QUOTA×CANDIDATE_PER_CATEGORY_MULT
#     在自己桶内取（见下方分桶配额），避免某类（被个人雷达 +4 拉高）霸占全局名额。

# ═══════════════════════════════════════════════════
#  决策侦察兵参数（Task 0 地基：供后续任务使用）
# ═══════════════════════════════════════════════════

FINAL_PICK = 13                          # triage 精选后最终保留条数
TRIAGE_MODEL = "deepseek-v4-pro"         # 决策环节用 V4-Pro 思考模式（吃判断力）
SCOUT_MODEL = "deepseek-v4-flash"        # 侦察兵 ReAct 循环用 V4-Flash 思考模式（多轮）
SCOUT_ENABLED = True                     # 侦察兵总开关，想省钱临时关掉改 False
SCOUT_MAX_ROUNDS = 6                     # ReAct 最多循环轮数
SCOUT_MAX_TOOL_CALLS = 10               # 工具调用总次数上限
SCOUT_FINDINGS = 3                       # 最终输出几条发现
SCOUT_SALVAGE = True                     # 保底挑选：finish 交空列表时放宽标准强补 1 条（防板块全空）
SEARCH_MONTHLY_CAP = 800                 # 月搜索调用上限（留余量给 1000 免费额度）
TOPIC_PLATFORM = "短视频/公众号通用"      # 自媒体选题平台口吻

# ═══════════════════════════════════════════════════
#  生物前沿单槽参数（独立板块，每期精确 1 条，不参与主新闻打分竞争）
# ═══════════════════════════════════════════════════
BIO_ENABLED = True                       # 生物板块总开关
BIO_MODEL = "deepseek-v4-flash"          # 生物一句话概括用 V4-Flash 思考模式
BIO_TIME_WINDOW_HOURS = 72               # 科研报道更新慢，时间窗放宽到 72h
BIO_MAX_PER_FEED = 6                      # 每个生物源最多取几条进候选

# ── 分类配额（事前分桶 · 硬隔离）──────────────────────────────
# 核心思想「真正解耦」：打分照旧（个人雷达/金融命中仍 +4），但「选稿」按分类各排各的——
# 每个分类只在【自己桶内】按分数排序、选满自己的配额，桶与桶之间零竞争。
# 这样个人雷达/金融的 +4 只在本类内部争座次，科技/财经再热也挤不掉「国际」的名额。
#
# CATEGORY_QUOTA：最终各类「上限」，之和 = FINAL_PICK(13)。某类当天不够就留空，绝不让别类来抢。
CATEGORY_QUOTA = {"国际": 6, "科技": 4, "财经": 3}
# CATEGORY_FLOOR：硬新闻「下限」——国际(地缘)/财经(宏观)必须覆盖；入选不足时从【本类】候选补足，不跨类。
CATEGORY_FLOOR = {"国际": 3, "财经": 2}
# 粗筛候选阶段：每类预留「配额 × 倍数」条进 AI 精选（给 R1 留挑选余地，仍是分桶不跨类）。
# ×3 而非 ×2：关键词分只是「便宜门卫」，难免漏判。把门开大一档，让 R1 多看一层兜底——
# 门卫漏掉的硬新闻还有机会被语义精选捞回。代价仅多读十几条轻量摘要，近乎免费。
CANDIDATE_PER_CATEGORY_MULT = 3
# 抓正文后，正文超过这个长度的条目额外加分（信息密度高）
FULLTEXT_LENGTH_BONUS = 800  # 字符数阈值
FULLTEXT_BONUS_SCORE = 1.5   # 超过阈值加的分
# 抓正文时的并发数和单条超时（秒）。并发快但别太猛，免得被网站当攻击。
FULLTEXT_WORKERS = 6
FULLTEXT_TIMEOUT = 12
# 喂给 AI 的正文最多保留多少字（控制 token 成本）。
# 1000 字约一两段，AI 看不到原文的数据/因果/引语 → 点评只能写空话。提到 2800（约一篇
# 中长报道全文），深度点评才有原料。截断发生在 trafilatura 抠出正文之后，加大只是少切
# 正文尾巴，不会引入广告/导航噪声。DeepSeek 上下文足够，每条多几百 token 成本可忽略。
# 想更省把数字改小。
FULLTEXT_MAX_CHARS = 2800

# 抓 RSS 源的超时（秒）和并发数。
# 旧实现 feedparser.parse(url) 内部下载无超时，任一源挂起会拖死整个 job。
FEED_FETCH_TIMEOUT = 15
FEED_FETCH_WORKERS = 8

# ── 分批阈值：每批最多发多少条给 DeepSeek ──
# 太多条 → prompt 大 → 服务端超时掐断。拆成小批每批独立写，最后拼起来。
BATCH_SIZE = 7

# ═══════════════════════════════════════════════════
#  RSS 新闻源（国际要闻 + 科技/AI + 财经市场）
# ═══════════════════════════════════════════════════
#
# 关于 "reference": True ──────────────────────────────
# 路透/AP/彭博这些顶级通讯社关闭了公开 RSS，只能走 Google News 代理，
# 而 Google News 的链接是加密跳转、抓不到正文全文。所以把它们标成「参考源」：
#   · 它们的报道只用来给新闻「投重要性票」（触发多源印证加分）
#   · 同一件事若也被能抓全文的源报道，代表作优先用那条深度源
#   · 只有某事仅被参考源报道时，才用它本身（浅，但重要的事不漏）
# 没标 reference 的都是直连真 RSS，能抓全文、有完整深度。
RSS_FEEDS = [
    # ── 国际要闻 ──
    {"name": "Reuters", "url": "https://news.google.com/rss/search?q=when:1d+site:reuters.com&hl=en-US&gl=US&ceid=US:en", "category": "国际", "reference": True},
    {"name": "AP", "url": "https://news.google.com/rss/search?q=when:1d+site:apnews.com&hl=en-US&gl=US&ceid=US:en", "category": "国际", "reference": True},
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml", "category": "国际"},
    {"name": "DW", "url": "https://rss.dw.com/xml/rss-en-world", "category": "国际"},
    {"name": "Nikkei Asia", "url": "https://asia.nikkei.com/rss/feed/nar", "category": "国际"},
    # ── 科技 / AI ──
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/", "category": "科技"},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage?points=100", "category": "科技"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "category": "科技"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "科技"},
    # ── 财经市场 ──
    {"name": "FT", "url": "https://www.ft.com/world?format=rss", "category": "财经"},
    {"name": "Reuters Business", "url": "https://news.google.com/rss/search?q=when:1d+site:reuters.com+(markets+OR+economy+OR+stocks+OR+earnings)&hl=en-US&gl=US&ceid=US:en", "category": "财经", "reference": True},
    {"name": "CNBC", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "category": "财经"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "财经"},
    # ── 2026-06-07 新增补充源 ──
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "国际"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss", "category": "国际"},
    {"name": "SCMP", "url": "https://www.scmp.com/rss/91/feed", "category": "国际"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "category": "科技"},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "category": "科技"},
    {"name": "Bloomberg", "url": "https://news.google.com/rss/search?q=when:1d+site:bloomberg.com&hl=en-US&gl=US&ceid=US:en", "category": "财经", "reference": True},
    {"name": "36kr", "url": "https://36kr.com/feed", "category": "科技"},
    # ── 第1层：一手信源 ──
    {"name": "美联储新闻稿", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "category": "财经", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "欧央行新闻稿", "url": "https://www.ecb.europa.eu/rss/press.html", "category": "财经", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "SEC 新闻稿", "url": "https://www.sec.gov/news/pressreleases.rss", "category": "财经", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "白宫公告", "url": "https://www.whitehouse.gov/presidential-actions/feed/", "category": "国际", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "arXiv cs.AI", "url": "https://rss.arxiv.org/rss/cs.AI", "category": "科技", "max_items": 3, "window_hours": SLOW_WINDOW_HOURS},
    {"name": "OpenAI 博客", "url": "https://openai.com/blog/rss.xml", "category": "科技", "max_items": 3, "window_hours": SLOW_WINDOW_HOURS},
    {"name": "DeepMind 博客", "url": "https://deepmind.google/blog/rss.xml", "category": "科技", "max_items": 3, "window_hours": SLOW_WINDOW_HOURS},
    {"name": "HuggingFace 博客", "url": "https://huggingface.co/blog/feed.xml", "category": "科技", "max_items": 3, "window_hours": SLOW_WINDOW_HOURS},
    {"name": "英伟达博客", "url": "https://blogs.nvidia.com/feed/", "category": "科技", "window_hours": SLOW_WINDOW_HOURS},
    # ── 第2层：高密度垂直源 ──
    {"name": "Stratechery", "url": "https://stratechery.com/feed/", "category": "科技", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "SemiAnalysis", "url": "https://semianalysis.com/feed/", "category": "科技", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "Import AI", "url": "https://importai.substack.com/feed", "category": "科技", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/", "category": "科技", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "Interconnects", "url": "https://www.interconnects.ai/feed", "category": "科技", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "ChinaTalk", "url": "https://www.chinatalk.media/feed", "category": "国际", "window_hours": SLOW_WINDOW_HOURS},
    {"name": "Pragmatic Engineer", "url": "https://newsletter.pragmaticengineer.com/feed", "category": "科技", "window_hours": SLOW_WINDOW_HOURS},
    # ── 第3层：社区信号 ──
    {"name": "r/MachineLearning", "url": "https://www.reddit.com/r/MachineLearning/top/.rss?t=day", "category": "科技"},
    {"name": "GitHub Trending", "url": "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml", "category": "科技"},
    # ── 个人雷达专属源 ──
    {"name": "Tom's Hardware", "url": "https://www.tomshardware.com/feeds/all", "category": "科技"},
    {"name": "Wccftech", "url": "https://wccftech.com/feed/", "category": "科技"},
    {"name": "GameDeveloper", "url": "https://www.gamedeveloper.com/rss.xml", "category": "科技"},
    {"name": "PC Gamer", "url": "https://www.pcgamer.com/rss/", "category": "科技", "max_items": 4},
]

# ═══════════════════════════════════════════════════
#  重要性打分用的关键词表（想调"什么算重要新闻"，改这里）
# ═══════════════════════════════════════════════════
#
# 思路：标题里出现这些"信号词"就加分，分数越高越可能进晨报。
# 权重越大代表越重要。全部用小写，匹配时不区分大小写。
#
# 高权重（+3）：重大地缘 / 货币政策 / 头部科技公司动作
HIGH_SIGNAL_KEYWORDS = [
    # 地缘与重大事件
    "war", "ceasefire", "invasion", "missile", "nuclear", "sanction",
    "coup", "election", "summit", "crisis", "attack", "strike",
    "earthquake", "typhoon", "flood", "wildfire", "protest", "crackdown",
    # 货币政策与宏观
    "fed", "rate cut", "rate hike", "inflation", "recession", "tariff",
    "central bank", "gdp", "default", "stimulus", "layoff", "bankruptcy",
    # 宏观数据发布（真正驱动市场的重磅事件，之前完全没覆盖）
    "cpi", "ppi", "pmi", "fomc", "payrolls", "unemployment", "jobs report",
    # 央行 / 利率决议
    "ecb", "boj", "pboc", "rate decision", "hawkish", "dovish", "debt ceiling",
    # 债券 / 收益率（用短语避免 bond/yield 单词歧义，并兼容单复数）
    "treasury yield", "treasury yields", "bond yield", "bond yields",
    # 头部科技 / AI
    "openai", "nvidia", "anthropic", "deepseek", "gpt", "chip ban",
    "semiconductor", "breakthrough", "claude", "gemini", "llm", "agi",
]
# 中权重（+1）：常规但有价值的商业 / 科技 / 市场新闻
MEDIUM_SIGNAL_KEYWORDS = [
    "ai", "apple", "google", "microsoft", "amazon", "tesla", "meta",
    "earnings", "merger", "acquisition", "launch",
    "market", "oil", "gold", "lawsuit", "deal", "ban",
    "startup", "funding", "regulation", "antitrust", "guidance",
    "ev", "battery", "solar", "fusion", "quantum",
    # 注：币圈 / 股票 / 理财相关词（crypto/stocks/ipo/buyback/dividend/nasdaq 等）
    #     已从这里（+1）上移到 PERSONAL_FINANCE_KEYWORDS（+4，个人金融喜好）
]
# 负权重（-2）：标题命中就降权（多半是软新闻 / 娱乐 / 凑数）
LOW_VALUE_KEYWORDS = [
    "recipe", "celebrity", "gossip", "royal", "horoscope", "fashion",
    "recap", "quiz", "best deals", "how to watch", "trailer",
    "tiktok", "viral video", "top 10", "unboxing", "reacts to",
    "rumor", "leak", "spotted",
]

# 个人雷达：读者私人关注领域，命中 +4（高于任何通用信号）
PERSONAL_KEYWORDS = [
    # 游戏 / 硬件
    "steam", "valve", "unreal engine", "unity",
    "rtx", "radeon", "dlss", "gpu price",
    # AI 编程 / 开源模型
    "claude code", "cursor", "copilot", "open source model",
    "local llm", "ollama", "fine-tuning", "api price",
    # AI Agent / 前沿 LLM
    "ai agent", "agentic", "multi-agent", "mcp", "tool calling",
    "function calling", "rag", "langchain", "langgraph",
    "crewai", "llama", "mistral", "vector database",
    "chain of thought", "prompt engineering", "rlhf",
    # 前沿科技 / CS
    "rust", "compiler", "wasm", "risc-v", "linux kernel",
    "postgresql", "distributed", "protocol", "cryptography",
    "programming language", "llvm", "typescript",
    "reverse engineering", "emulator",
]

# 个人金融喜好：偏「投资 / 理财」视角——币圈 + 股票 + 个人理财，整个金融范畴都算。
# 命中 +4（与个人雷达同级）。这些词原先散落在 MEDIUM_SIGNAL（+1），现统一上移到个人喜好层。
# 安全性：因为「选稿」改成了分类硬分桶（见 CATEGORY_QUOTA），金融 +4 只在财经桶内部排座次，
# 不会把国际/科技的名额挤掉——所以可以放心把金融加分调到和个人雷达一样高。
PERSONAL_FINANCE_KEYWORDS = [
    # 币圈 / Web3
    "crypto", "bitcoin", "btc", "ethereum", "eth", "blockchain",
    "defi", "web3", "solana", "stablecoin", "altcoin", "memecoin",
    "halving", "on-chain", "crypto etf", "spot etf",
    # 股票 / 投资标的
    "stocks", "ipo", "buyback", "dividend", "nasdaq", "s&p 500",
    "etf", "index fund", "mutual fund", "hedge fund", "reit",
    "short squeeze", "options trading", "tokenization", "valuation",
    # 个人理财 / 财富管理
    "personal finance", "wealth management", "passive income",
    "401k", "roth ira", "brokerage", "portfolio",
    "asset allocation", "financial freedom",
]

# 个人喜好合集：个人雷达 + 个人金融，一起喂给 _PERSONAL_RE（同为 +4）。
# 这样粗筛打分时「金融/投资/理财」与「游戏/AI/CS」享受同一档个人权重。
PERSONAL_ALL_KEYWORDS = PERSONAL_KEYWORDS + PERSONAL_FINANCE_KEYWORDS

# ═══════════════════════════════════════════════════
#  读者画像（侦察兵与粗筛共用的「同一份口味」）
# ═══════════════════════════════════════════════════
#
# 为什么要有这段散文：粗筛靠上面的关键词表命中加分（机器视角），而侦察兵 Agent
# 需要一段「人话」才能理解该替谁去挖信息差。两者必须是同一个人——否则侦察兵搜的
# 人设会比粗筛打分的人设窄（历史上 scout 只写了「游戏+AI编程」，漏了 Agent/前沿CS/币圈）。
# 改读者口味时，关键词表与这段画像一起改，保持同步。scout.py 直接 import 这个常量。
READER_PROFILE = """一位身处中国的硬核技术读者，关注领域（按个人喜好排序）：
· 硬核 PC 游戏 / MOD 玩家：Steam/Valve、显卡（RTX/Radeon/DLSS、GPU 价格）、游戏引擎（Unreal/Unity）。
· 正在学 AI 编程：Claude Code/Cursor/Copilot、本地大模型（Ollama/开源模型）、微调、API 定价。
· AI Agent / 前沿 LLM：agentic、multi-agent、MCP、工具调用/function calling、RAG、LangChain/LangGraph、prompt engineering。
· 前沿科技 / CS：Rust、编译器/LLVM、WASM、RISC-V、Linux 内核、分布式系统、密码学、逆向工程、模拟器。
· 金融 / 投资 / 理财（偏个人投资视角，整个金融范畴都关注）：币圈（比特币/以太坊/DeFi/Web3/稳定币/链上动态）、股票与 ETF/指数基金、个人理财与资产配置，关注真实可操作的投资机会与市场信号。"""

# ═══════════════════════════════════════════════════
#  生物前沿：专源 + 关键词（独立单槽，不混入上面的通用打分池）
# ═══════════════════════════════════════════════════
#
# 思路：主新闻三大类（国际/科技/财经）之外，每期单独挑 1 条「生物/医学科研突破」。
# 这些源只喂给 digest/bio.py，不进 RSS_FEEDS 主流程，故与主打分完全隔离。
BIO_FEEDS = [
    {"name": "ScienceDaily 健康", "url": "https://www.sciencedaily.com/rss/top/health.xml"},
    {"name": "ScienceDaily 生物", "url": "https://www.sciencedaily.com/rss/plants_animals.xml"},
    {"name": "Nature", "url": "https://www.nature.com/nature.rss"},
    {"name": "Phys.org 生物", "url": "https://phys.org/rss-feed/biology-news/"},
    {"name": "STAT News", "url": "https://www.statnews.com/feed/"},
    {"name": "New Scientist 健康", "url": "https://www.newscientist.com/subject/health/feed/"},
    # EurekAlert 改版后整站反爬（旧 RSS 路径全 403/404），换 Science X 旗下稳定的 MedicalXpress
    {"name": "MedicalXpress 医学", "url": "https://medicalxpress.com/rss-feed/"},
]

# 生物突破信号词：命中越多越像「重磅科研突破」。整词匹配（\b），全小写。
BIO_KEYWORDS = [
    # 基因 / 分子
    "crispr", "gene therapy", "gene editing", "genome", "dna", "rna", "mrna",
    "mutation", "protein folding", "alphafold", "enzyme", "synthetic biology",
    # 细胞 / 再生
    "stem cell", "organoid", "cell therapy", "regeneration", "embryo", "tissue",
    # 医学 / 临床
    "clinical trial", "vaccine", "antibody", "immunotherapy", "cancer", "tumor",
    "drug", "fda approval", "biomarker", "longevity", "aging",
    # 神经 / 微生物
    "neuron", "brain", "microbiome", "bacteria", "virus",
    # 信号词
    "breakthrough", "discovery", "first-ever", "novel",
]

# 生物源信任分（粗排用，影响很小，主要靠关键词命中拉分）
BIO_SOURCE_TRUST = {
    "Nature": 3, "ScienceDaily 健康": 2, "ScienceDaily 生物": 2,
    "STAT News": 2, "New Scientist 健康": 2, "MedicalXpress 医学": 2,
    "Phys.org 生物": 1,
}

# ═══════════════════════════════════════════════════
#  来源可信度分级（想调「更信哪家媒体」，改这里的数字）
# ═══════════════════════════════════════════════════
#
# 思路：硬新闻、调查能力强的源加分；聚合/快讯类给 0 或降权。
# 这是「主编的口味」，没有标准答案——下面是一套默认值，你随时改数字即可。
# key 要和上面 RSS_FEEDS 里的 name 完全一致；没列到的源默认 0 分。
SOURCE_TRUST = {
    # ── 国际：一线通讯社 / 硬新闻（+2），日经亚洲（+1）──
    "Reuters": 2,
    "AP": 2,
    "BBC World": 2,
    "DW": 2,
    "Nikkei Asia": 1,
    # ── 科技：MIT/HN 顶级（+2），Ars/Verge 专业（+1）──
    "MIT Tech Review": 2,
    "Hacker News": 2,
    "Ars Technica": 1,
    "The Verge": 1,
    # ── 财经：FT/路透财经（+2），CNBC/CoinDesk（+1）──
    "FT": 2,
    "Reuters Business": 2,
    "CNBC": 1,
    "CoinDesk": 1,
    # ── 2026-06-07 新增源 ──
    "Al Jazeera": 2,
    "The Guardian": 2,
    "SCMP": 1,
    "TechCrunch": 1,
    "Wired": 1,
    "Bloomberg": 2,
    "36kr": 1,
}

SOURCE_TRUST.update({
    # 第1层 一手信源
    "美联储新闻稿": 3, "欧央行新闻稿": 3, "SEC 新闻稿": 3, "白宫公告": 2,
    "arXiv cs.AI": 3, "OpenAI 博客": 3, "DeepMind 博客": 3,
    "HuggingFace 博客": 2, "英伟达博客": 2,
    # 第2层 垂直分析
    "Stratechery": 3, "SemiAnalysis": 3, "Import AI": 3, "Simon Willison": 2,
    "Interconnects": 2, "ChinaTalk": 2, "Pragmatic Engineer": 2,
    # 第3层 社区
    "GitHub Trending": 1,  # HN/Reddit 已在原表或按默认
    "r/MachineLearning": 2,
    # 个人雷达游戏/硬件源（基础分低，靠 PERSONAL_KEYWORDS +4 拉起来）
    "Tom's Hardware": 1, "Wccftech": 0, "GameDeveloper": 1, "PC Gamer": 0,
})

# 标题党 / 软文句式：标题命中其一就降权（-2）。用正则匹配。
CLICKBAIT_PATTERNS = [
    r"\bhere'?s why\b",          # "Here's why ..."
    r"\bhere'?s what\b",         # "Here's what ..."
    r"^\d+\s+(things|ways|reasons|signs)\b",   # "7 things you should..."
    r"\byou (won'?t|wont) believe\b",
    r"\bthis is (why|how|what)\b",
    r"\bwhat to know\b",
    r"\bwe (tried|tested|ranked)\b",
    r"\?\s*$",                   # 整条标题以问号结尾，多半是钓鱼式标题党
]

# ═══════════════════════════════════════════════════
#  实锤信号（补关键词词典的两个盲区，纯加分，不影响既有打分）
# ═══════════════════════════════════════════════════
#
# 关键词分只认「词典里有没有这个词」，认不出两件事：
#   ① 标题有没有「具体数字」——硬新闻多带金额/百分比/数量级，软文很少带。
#   ② 标题是「具体事件」还是「泛泛议题」——前者专有名词多。
# 这两维专门补盲，只加分、不减分，所以不会撞「关键词线性叠加」那条既有测试。
#
# 实锤数字：标题含金额/百分比/量级（$2B、40%、3 billion）→ +1。
# 用「符号/量级词」上下文判断，而非裸数字——避免把年份(2026)当数据误命中。
HARD_NUMBER_BONUS = 1.0
_HARD_NUMBER_RE = re.compile(
    r"[\$€£¥]\s?\d"                                   # 货币符号紧跟数字：$2、€1.5
    r"|\d+(?:[.,]\d+)?\s?%"                           # 百分比：40%、3.5 %
    r"|\b\d+(?:[.,]\d+)?\s?(?:billion|million|trillion|bn|mn|percent)\b",  # 量级词
    re.IGNORECASE,
)

# 实体密度：标题含 ≥ENTITY_DENSITY_MIN 个专有名词 → 是「具体事件」而非「泛泛议题」，+1。
# 专有名词抽取复用 scoring.extract_proper_nouns（同一套逻辑，聚类也在用）。
ENTITY_DENSITY_MIN = 2
ENTITY_DENSITY_BONUS = 1.0

# ═══════════════════════════════════════════════════
#  关键词正则预编译（性能 + 防子串误命中）
# ═══════════════════════════════════════════════════
#
# 旧实现用 `kw in text` 做子串匹配，会把：
#   · "ai"  误命中 said / again / rain  (+1 噪声)
#   · "war" 误命中 warning / award / software  (+3 噪声 ❗)
#   · "ev"  误命中 every / even / level / never
#   · "ban" 误命中 bank / urban；"oil" 误命中 boil / turmoil
# 全部当成真信号加分。改用 \b 单词边界正则杜绝此类误匹配。
# 性能：每条新闻打分时只需 3 次 regex.findall 而非 N 次 `in`。
def _compile_keyword_pattern(keywords: list[str]) -> "re.Pattern[str]":
    """把关键词列表编译成单个 \\b(kw1|kw2|...)\\b 正则（整词匹配，已含 IGNORECASE）。"""
    if not keywords:
        return re.compile(r"(?!)")  # 永不匹配的哨兵正则
    escaped = "|".join(re.escape(kw) for kw in keywords)
    return re.compile(rf"\b(?:{escaped})\b", re.IGNORECASE)


_HIGH_SIGNAL_RE = _compile_keyword_pattern(HIGH_SIGNAL_KEYWORDS)
_MEDIUM_SIGNAL_RE = _compile_keyword_pattern(MEDIUM_SIGNAL_KEYWORDS)
_LOW_VALUE_RE = _compile_keyword_pattern(LOW_VALUE_KEYWORDS)
_PERSONAL_RE = _compile_keyword_pattern(PERSONAL_ALL_KEYWORDS)
_BIO_SIGNAL_RE = _compile_keyword_pattern(BIO_KEYWORDS)

# ═══════════════════════════════════════════════════
#  聚类用的高频虚词（这些词到处都是，不能用来判断「是否同一件事」）
# ═══════════════════════════════════════════════════
TITLE_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "at", "by",
    "with", "from", "as", "is", "are", "was", "were", "be", "been", "will",
    "says", "after", "over", "amid", "into", "out", "up", "new", "us", "uk",
    "report", "live", "news", "video", "watch", "update", "latest",
}

# ═══════════════════════════════════════════════════
#  GitHub 热榜板块（独立板块，每期 5 条，不参与主新闻打分竞争）
# ═══════════════════════════════════════════════════
GITHUB_ENABLED = True                          # 板块总开关
GITHUB_MODEL = "deepseek-v4-flash"             # 中文一句话用 V4-Flash 思考模式
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

# ═══════════════════════════════════════════════════
#  语义去重（词面聚类之上叠加的向量精筛层）
# ═══════════════════════════════════════════════════
#
# DeepSeek 无 embedding 接口，故用本地 fastembed（onnxruntime，无 torch）。
# 装不上 / 模型下载失败时 embed_titles 返回 None → 整层自动跳过，回退纯词面聚类。
SEMANTIC_DEDUP_ENABLED = True
# 多语言小模型：36kr 是中文源、标题中英混合，必须多语言。
EMBED_MODEL = "intfloat/multilingual-e5-small"
# 余弦相似度 ≥ 此值判同事件。0.86 偏稳；更激进合并降到 0.82，更保守升到 0.90。
SEMANTIC_SIM_THRESHOLD = 0.86
# 编码文本是否拼接 summary 前若干字（提升短标题区分度）。
SEMANTIC_USE_SUMMARY = True
SEMANTIC_SUMMARY_CHARS = 120

# ═══════════════════════════════════════════════════
#  自评重写环（LLM-as-judge：DeepSeek 审自己的成稿）
# ═══════════════════════════════════════════════════
#
# 成稿后让 DeepSeek 打分，低于阈值带着问题清单重写一次。
# 重写后做「来源数不降 + 长度不暴跌」硬校验，破格式就弃用——质量增强绝不阻断推送。
SELF_CRITIQUE_ENABLED = True
CRITIQUE_MODEL = "deepseek-v4-pro"   # 裁判吃判断力，用 V4-Pro 思考模式
REWRITE_THRESHOLD = 6.0              # overall < 此分触发重写（保守起步，少重写省 token）
REWRITE_MAX = 1                      # 最多重写次数（防烧钱/死循环）
