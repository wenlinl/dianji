# 惦记 · 千里之外，也在身边

面向中老年用户的**手机操作求助与家庭协同 Agent**。长辈手机卡住时，只按一个按钮，对着 AI 说就行；AI 用大白话一步步教、每步念出来；搞不定一键转给绑定的子女接手。

> 主题：为外公做一个刚好够用的 Agent

## 在线体验

- H5 主交付：https://dianji.online/h5/
- 自建 MCP（Streamable HTTP）：https://dianji.online/mcp

演示账号：

| 身份 | 账号 | 密码 |
| --- | --- | --- |
| 外公·李建国 | laoren | 123456 |
| 外婆·王桂兰 | laopo | 123456 |
| 女儿·李薇 | zinu | 123456 |
| 孙女·李小雅 | sunnv | 123456 |

## 功能

- **老人端**：一个「我卡住了」大按钮 → 语音 / 截图 / 拍照求助 → AI 生成 ≤3 步大白话指引（0.3~7 秒返回，流式朗读）→ 说话指挥（"再讲一遍/好了/卡住了/叫家里人"）→ 一键转子女（可选择发给哪个后辈）→ 状态实时同步 + 撤回
- **子女端**：长辈切换（外公/外婆数据完全独立）→ 收件箱 / 提醒 / 备忘录 / 教程 四页底部导航；回复、标记解决、视频教他（一键拨打）、教程沉淀（同类问题自动复用）
- **多家庭**：多对多绑定，不同长辈数据隔离、不同后辈收件箱隔离
- **适老化**：大字模式、高对比模式、自动朗读、图片自动压缩、响应式布局
- **MCP**：`dianji_phone_help`（操作指引）、`dianji_risk_check`（风险初判）

## 技术架构

- 前端：单页 H5（HTML/CSS/JS），无框架，可添加到主屏幕
- 后端：Python 标准库 HTTP 服务 + SQLite，nginx 反代 + HTTPS
- AI：豆包视觉大模型（关闭思考模式提速 20 倍）、火山引擎语音识别（ASR）、豆包语音合成（TTS 流式朗读）

## 目录结构

```text
h5/
  index.html        前端（登录、老人端、子女端、适老化）
  server.py         后端（SQLite、登录、AI、ASR、TTS、MCP 之外的全部 API）
  mcp/              MCP Server（Streamable HTTP）
assets/             界面素材与生图
*.md                产品文档（评审清单、交互流程、素材清单等）
```

## 本地运行

```bash
# 需要 Python 3 标准库 + ffmpeg（语音识别转码）
cp .env.example .env   # 配置 READ_IMAGE_API_KEY 等
python3 h5/server.py   # http://localhost:8000/h5/
```

密钥仅通过环境变量注入（`~/.codex/read-image.env` 或系统环境），不随代码提交。
