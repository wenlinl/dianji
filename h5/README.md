# 惦记 · H5 演示版（本地运行）

## 启动

```bash
python3 h5/server.py
```

打开 http://localhost:8000/h5/

## 依赖

- 仅 Python 3 标准库；
- 需要 `~/.codex/read-image.env` 里的火山方舟密钥（`READ_IMAGE_API_KEY` / `READ_IMAGE_BASE_URL` / `READ_IMAGE_MODEL`），用于 `/api/help` 调用豆包视觉模型生成指引。

## 功能

- 老人端：语音/上传截图/摄像头求助 → AI 生成 ≤3 步指引（语音朗读）→ 转子女；
- 子女端：收件箱 → 回溯（截图+步骤）→ 回复 → 标记已解决；
- 提醒：打开页面展示待办（演示数据）。

## 说明

- 求助记录存在后端内存中，重启服务即清空（演示用）；
- H5 无实时屏幕共享与系统悬浮窗，按设计以"看一步做一步 + 语音朗读"呈现；
- 语音输入依赖浏览器 Web Speech API（Chrome 桌面/Android 支持较好）；朗读用 speechSynthesis。
