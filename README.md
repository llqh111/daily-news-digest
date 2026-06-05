# 📰 每日全球要闻推送

每天自动抓取国际新闻 RSS → **DeepSeek AI** 总结为中文 → **Server酱** 推送到微信。

## 工作原理

```
RSS Feed (BBC/Guardian/NPR/CNBC...) → DeepSeek 中文摘要 → 微信消息
```

## 配置

1. Fork 此仓库
2. 在 Settings → Secrets 中设置：
   - `DEEPSEEK_API_KEY` — DeepSeek API 密钥
   - `SERVERCHAN_SENDKEY` — Server酱 SendKey
3. GitHub Actions 每天北京时间 8:00 自动运行

## 本地测试

```bash
pip install -r requirements.txt
cp .env.example .env  # 编辑填入密钥
python diagnose.py     # 诊断检测
python main.py         # 运行
```
