# 日报稳定性修复（2026-10-11）

## 当前结论

修复已发布至 release，并通过本地及线上 181 项确定性测试。
真实日报已于北京时间 11:18 完成推送，4 位微信收件人接口回执全部成功；完整中文正文及送达/证据/质量归档已验证。长期稳定率仍需后续定时运行验证。

## 生产证据

- 2026-09-14 至 2026-09-25 的 08:00 日报运行连续成功（12 次）。
- 2026-09-26 至 2026-10-11 的 08:00 日报运行仅 10-06 和 10-10 成功（2/16）。
- 历史余额/请求格式故障此前已处理；本次聚焦修复后仍发生的生成与解析故障。
- [10-07](https://github.com/llqh111/daily-news-digest/actions/runs/37549634732)：DeepSeek HTTP 400 Content Exists Risk。现有生产修复已提供原始来源快讯回退；本次保留该行为，回归验证拒绝后不重发被拒素材。
- [10-08](https://github.com/llqh111/daily-news-digest/actions/runs/37705411145)：正文抓取开始后出现 malloc_consolidate(): unaligned fastbin chunk detected，随后 exit 134。日志不足以确定具体原生库。
- [10-09](https://github.com/llqh111/daily-news-digest/actions/runs/37862512026)：DigestLayoutError，缺少 article_id、正文段落与来源。
- [10-11 08:00](https://github.com/llqh111/daily-news-digest/actions/runs/38096946890)：DigestLayoutError，主新闻 article_id 缺失、重复或被替换。
- [10-11 09:00](https://github.com/llqh111/daily-news-digest/actions/runs/38100359065)：实际执行 python main.py --weekly，微信 4 人成功；此记录不能证明日报恢复。
- Telegram 目前返回 Unauthorized；本次不改变凭据，微信是已确认有效的主通道。

## 修复范围

1. 写稿不再重复筛选上游已经入选的新闻；明确要求 ID 原样保留一次。
2. 每批逐条检查 ID、三段正文与原始来源；仅损坏/缺失条目单独补写一次。补写仍不完整则继续阻断，不猜测 ID，不把缺稿当成功。
3. 板块按输入分类在程序中拼接，保留三大标题。单批修复时排除旧编辑手记，避免混入新闻段落。
4. 正文下载保持并发，HTML 原生解析放在独立子进程；解析进程异常退出或超时则仅该篇回退到 RSS 摘要。这隔离了解析进程的崩溃影响，未对原生库根因作确定归因。
5. 生产测试入口加入 test_ai.py，避免流式调用回归测试遗漏。

## 验证

命令：python -m pytest test_core.py test_ai.py test_semantic_dedup.py test_critique.py test_triage.py tests -q

本地为 Python 3.14.5，生产为 Python 3.12。沙箱临时目录权限不足时使用工作树内专用 --basetemp 运行。

- 181 passed in 1.79s。
- git diff --check 通过。
- 漏 ID、错误 ID、重复 ID、错误来源补写覆盖；补写失败仍阻断。
- 解析异常退出/超时的模拟检查通过，真实子进程中文 HTML 解析通过。
- 既有内容拒绝回退、送达判定与去重行为检查通过。
- 按正确性、可读性、模块依赖、安全与性能完成复核：补写次数有界，HTML 经标准输入交给固定 Python 脚本，不使用 shell；无新依赖，无凭据改动。

## 上线验收与回退

发布前基线：e0482bd（包含最新送达记录）。
发布后使用 release 的生产 secrets 跑一次 daily，force=false；若本期已送达，保留去重，不强制重复发送。
确认完整中文正文、article_id 全覆盖、质量/送达归档及微信回执，不以 Actions success 单独作为验收。
长期稳定需观察后续定时日报；单次成功不等于恢复原有稳定率。
必要时仅 revert 本次两个代码修复提交，保留最新送达及去重记录，避免整分支回滚引起重发。

## 本次上线结果

- 修复提交：dfc909e、76bb452；生产送达归档提交：35e8cff。
- [生产 run 38107490080](https://github.com/llqh111/daily-news-digest/actions/runs/38107490080)：success，8 分 27 秒，daily 模式，force=false。
- 线上测试：181 passed in 1.16s。
- 上游选稿 13 条；正文解析获取 9/13 篇全文，8 篇达到深度加分线。
- 第二批实际发现并修复 3 条损坏稿件：1 条来源缺失、1 条 ID 缺失/重复、1 条来源链接被替换。补写后通过完整性检查；未降级为原始英文来源快讯。
- 11:17:50 至 11:17:59：4 位微信收件人均 success，1 段/人。
- 送达账本：2026-10-11-AM，delivered_at=2026-10-11T11:18:08.166983+08:00，evidence_status、quality_status、artifact_bundle_status 均为 present。
- 稿件：digests/2026-10-11-AM.md；证据：digests/meta/2026-10-11-AM-evidence.json；质量：digests/quality/2026-10-11-AM.json。
- 最终稿 13 条主新闻、13 行来源；证据和质量报告各 13 个 ID，集合完全对应，无缺失证据或 unmatched ID。无内部 ID、审计文本或占位符泄漏。
- 质量检查 validation_complete=true，但仍有 2 条数字证据提示（12/309、20/32 未匹配到对应证据卡数字）。这只是机械检查的提示，未在本次任务中作独立事实核查，不宣称事实质量零问题。
- Telegram 仍为 Unauthorized，未修复该凭据；微信成功与 Telegram 失败分别记录。
- 此次验证只发送了一次真实日报，未强制补发已送达班次，也未更改原有调度。
