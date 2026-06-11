"""每日全球要闻推送 —— 内部包。

主入口仍是项目根目录的 main.py（保留以兼容 README/CI/批处理）。
本包按职责拆分核心模块：

    config       配置：RSS 源、关键词表、信任分、各种常量
    storage      持久化：sent_links、digest 存档、session 去重
    scoring      打分与聚类：score_importance / cluster_and_boost / 分类均衡
    fetch        抓取：RSS 并发下载 + 正文 trafilatura 提取
    factcheck    事实核查：数字提取、跨文章冲突、输出端 sanity_check
    ai           AI 调用：prompt 构建 + DeepSeek 流式调用 + 分批合并
    push         推送：Server酱、Telegram、失败告警

兼容性约定：main.py 顶部会从这些模块 re-export 关键函数名，
保持 `from main import score_importance` 等旧调用方式可用，
现有 test_core.py / diagnose.py 无需修改。
"""
