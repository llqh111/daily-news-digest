# 日报稳定性修复验收（2026-10-25）

状态：修复 648fa30 已发布，真实日报验证通过；尚未经过连续一个月生产观察。
基线：origin/release f30423b；保留已上线的 80f7b2d 来源快讯降级。

## 线上证据

- [10 月 23 日失败](https://github.com/llqh111/daily-news-digest/actions/runs/39875430130)：补写后来源仍缺失，DigestLayoutError 阻断发送。
- [10 月 20 日失败](https://github.com/llqh111/daily-news-digest/actions/runs/39345029164)：同类来源缺失故障；Telegram 告警返回 Unauthorized。
- 10 月 25 日运行列表显示成功，不计作本次修改的生产验收。

## 本次修改

1. 备用选稿遵循分类配额，防止科技高分挤掉国际、财经。
2. API 空 choices、null 正文等异常进入已有来源快讯兜底；SSE 非字符串片段不拼接。
3. 重写后验证完整结构和原始来源行，损坏时保留合格原稿。
4. Telegram 异常不阻断微信成功后的去重台账保存。
5. 台账原子替换；写入失败明确报错，保留旧文件。
6. 工作流主流程失败后仍保存现有台账；Git push 最多三次；生成步骤上限 35 分钟，job 上限 45 分钟。

工作流依据：[GitHub 状态表达式文档](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions)。
不增加整期自动重跑或新定时源，避免在送达状态不确定时重发。

## 验证

- 新增缺陷用例先复现失败，再验证修复。
- 254 项离线测试通过；排除访问网络的 test_rss.py。
- 30 个模拟日期运行 main + 真实本地台账：AI 超时、内容拒绝、6 次全渠道失败、每天重复触发。
- 36 次模拟发送尝试、30 次成功；失败可重试，成功后重复触发不重发；逐日稿件和台账存在。
- git diff --check、工作流 YAML 解析通过。
- 本地 Python 3.14.5；生产 Python 3.12 尚需 CI 验证。

```powershell
python -m pytest --ignore=test_rss.py -q -p no:cacheprovider --basetemp=.task-tests-check
```

## 上线及一个月验收

用户已授权发布和一次真实外发；仅发布本修复提交，原工作区未提交内容未合入。
上线后验证 daily、force=false 的实际稿件、每位微信收件人回执、去重台账、evidence/quality 归档。
连续 30 个应发日期均有完整微信送达才算月度验收通过；来源快讯降级次数另计。

剩余边界：触发缺失、服务长期不可用、余额和凭据错误不能由代码保证解决。
当前契约仍为至少一个渠道成功，无跨运行逐收件人续传，部分收件人失败需人工处理。
渠道接收请求后响应丢失时，网络重试仍可能重发，不能承诺严格 exactly-once。
Git 同步三次仍失败或 runner 硬中止时，须核对回执并恢复台账后再重跑。
自动审批拒绝新增包含稿件和台账的 GitHub Actions 附件上传；该方案已移除。

回滚：在生产分支 git revert 本修复提交，保留生产台账，不 reset 到历史状态。

## 真实生产验收

- [运行 40160139995](https://github.com/llqh111/daily-news-digest/actions/runs/40160139995)：success，执行代码 648fa30，daily 模式。
- 因今天 08:07 已发过早报，本次已授权验证使用一次 force=true；定时任务默认值未改。
- 生产 Python 3.12 测试：198 passed in 1.58s（工作流测试范围小于本地完整 254 项）。
- 北京时间 12:06 微信四位收件人均返回成功回执，4 人各 1 段；本轮未使用来源快讯降级。
- Telegram 返回 Unauthorized，仍需修复凭据；未影响微信送达和台账保存。
- 远程台账提交 da4ba7d，updated=2026-10-25 12:06；稿件、evidence、quality 和 reps 已保存。
- 13 个主新闻 ID 在 evidence/quality/reps 中一致；validation_complete=true、artifact_bundle_status=present。
- 成稿没有 article_id、自我审计或占位符泄漏；14 条来源行包含附加板块来源。
- 质量报告有 6 条数字证据匹配提示，缺失证据条目为 0；匹配提示并不等于已证实错误，也不代表事实质量完全通过。
