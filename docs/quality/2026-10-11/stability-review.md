# 日报稳定性修复（2026-10-11）

## 当前结论

本地修复已通过生产工作流所运行的确定性测试：181 passed。
尚未发布至 release，尚未执行本次修复后的真实推送，不能宣称已恢复长期稳定。

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
