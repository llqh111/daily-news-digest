# 日报稳定性修复验收（2026-10-25）

状态：本地修复完成，未发布；离线测试不能证明连续一个月生产稳定。
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

发布和真实外发待授权；只发布本修复提交，不合入原工作区未提交内容。
上线后验证 daily、force=false 的实际稿件、每位微信收件人回执、去重台账、evidence/quality 归档。
连续 30 个应发日期均有完整微信送达才算月度验收通过；来源快讯降级次数另计。

剩余边界：触发缺失、服务长期不可用、余额和凭据错误不能由代码保证解决。
当前契约仍为至少一个渠道成功，无跨运行逐收件人续传，部分收件人失败需人工处理。
渠道接收请求后响应丢失时，网络重试仍可能重发，不能承诺严格 exactly-once。
Git 同步三次仍失败或 runner 硬中止时，须核对回执并恢复台账后再重跑。
自动审批拒绝新增包含稿件和台账的 GitHub Actions 附件上传；该方案已移除。

回滚：在生产分支 git revert 本修复提交，保留生产台账，不 reset 到历史状态。
