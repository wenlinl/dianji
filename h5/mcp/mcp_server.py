#!/usr/bin/env python3
"""惦记 · Dianji MCP Server (Streamable HTTP)

把「惦记」Agent 的核心能力暴露为标准 MCP 工具：
- dianji_phone_help   手机操作指引（文字 + 可选截图）→ ≤3 步大白话
- dianji_risk_check   可疑短信/链接/界面风险初判

依赖：pip install mcp
运行：MCP_HOST=127.0.0.1 MCP_PORT=8001 python3 mcp_server.py
环境变量：READ_IMAGE_API_KEY / READ_IMAGE_BASE_URL / READ_IMAGE_MODEL
"""

import base64
import json
import os
import re
import ssl
import urllib.request

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


def load_env():
    p = os.path.expanduser("~/.codex/read-image.env")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
API_KEY = os.environ.get("READ_IMAGE_API_KEY")
BASE_URL = os.environ.get("READ_IMAGE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
MODEL = os.environ.get("READ_IMAGE_MODEL", "doubao-seed-2-1-turbo-260628")

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CTX = ssl.create_default_context()


HELP_PROMPT = (
    "你是'惦记'App 里的适老化手机教学助手，服务对象是 70 岁左右、不太会用智能手机的长辈。"
    "根据用户的口头描述和/或截图，判断 TA 想做什么，然后给出最多 3 步、每步一句话的大白话操作指引。"
    "要求：只说动作，不说术语；每步以'你'开头；步骤之间不要编号以外的废话；"
    "如果看不清截图就明确说'我没看清你屏幕上的字，请重新拍一张'。"
    "只输出 JSON，格式：{\"steps\":[\"第一步…\",\"第二步…\"],\"note\":\"一句话说明\"}。"
)

RISK_PROMPT = (
    "你是'惦记'App 里的反诈风险初判助手，服务对象是 70 岁左右的长辈。"
    "根据用户描述的短信/微信消息内容或上传的截图，判断是否存在诈骗风险。"
    "要求：结论先说'要小心'或'基本安全'；再用最多 3 条大白话说明判断理由和该怎么做；语气温和不吓人。"
    "如果内容模糊看不清，就建议先不要点任何链接，并转给子女确认。"
    "只输出 JSON，格式：{\"verdict\":\"要小心/基本安全/看不太清\",\"reasons\":[\"…\"],\"advice\":\"一句话建议\"}。"
)


def call_ark(prompt, text, image_base64=None):
    content = [{"type": "text", "text": text}]
    if image_base64:
        b64 = image_base64
        mime = "image/jpeg"
        m = re.match(r"data:image/(\w+);base64,(.+)", image_base64, re.S)
        if m:
            mime, b64 = f"image/{m.group(1)}", m.group(2)
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    payload = {
        "model": MODEL,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "max_tokens": 600,
    }
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=90, context=_CTX) as resp:
        data = json.load(resp)
    raw = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("模型未返回 JSON")
    return json.loads(m.group(0))


_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["dianji.online", "dianji.online:*", "127.0.0.1:*", "localhost:*"],
)
mcp = FastMCP("惦记 Dianji", transport_security=_SECURITY)


@mcp.tool()
def dianji_phone_help(text: str, image_base64: str = "") -> str:
    """为不会用智能手机的长辈生成手机操作指引：输入 TA 想做的事（如“把微信文章转发到家族群”），可附上手机截图 base64，返回最多 3 步大白话操作指引。"""
    result = call_ark(HELP_PROMPT, text, image_base64 or None)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def dianji_risk_check(text: str = "", image_base64: str = "") -> str:
    """对长辈收到的可疑短信、微信消息、陌生链接或界面截图做风险初判：输入消息内容或截图 base64，返回风险结论、判断理由与建议。"""
    result = call_ark(RISK_PROMPT, text or "请看一下这张截图有没有风险", image_base64 or None)
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("未配置 READ_IMAGE_API_KEY")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8001"))
    mcp.settings.host = host
    mcp.settings.port = port
    print(f"惦记 MCP (Streamable HTTP) 已启动: http://{host}:{port}/mcp/")
    mcp.run(transport="streamable-http")
