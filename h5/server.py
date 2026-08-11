#!/usr/bin/env python3
"""惦记 H5 · 生产后端

功能：
- 静态托管 h5/ 页面（根目录=工作区）
- SQLite 数据库：用户/会话/求助/提醒/备忘录（首次启动自动建表并写入演示账号）
- 登录系统：POST /api/login → token；Bearer 鉴权
- POST /api/help    老人求助 → 豆包视觉模型生成 ≤3 步指引
- POST /api/asr     录音转文字（火山引擎极速版语音识别，需 ffmpeg）
- POST /api/forward 转子女 → 写库
- GET  /api/requests 子女收件箱 / 老人自己的求助
- POST /api/reply /api/status 子女回复、更新状态
- GET/POST /api/reminders 提醒（子女录入，老人标记完成）
- GET/POST /api/memos 家庭备忘录

环境变量：
- READ_IMAGE_API_KEY / READ_IMAGE_BASE_URL / READ_IMAGE_MODEL（豆包视觉）
- VOLC_SPEECH_API_KEY / VOLC_SPEECH_APP_ID（火山语音识别，可选）
- DIANJI_DB（数据库路径，默认工作区 dianji.db）
"""

import base64
import datetime
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DIANJI_DB", os.path.join(ROOT, "dianji.db"))
UPLOAD_DIR = os.environ.get("DIANJI_UPLOAD_DIR", os.path.join(ROOT, "uploads"))
_DB_LOCK = threading.Lock()
_TTS_CACHE = {}
_TTS_CACHE_ORDER = []
os.makedirs(UPLOAD_DIR, exist_ok=True)


def load_env_file(path):
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env_file(os.path.expanduser("~/.codex/read-image.env"))
load_env_file(os.path.expanduser("~/Documents/ChatGPT/midyear-workshop/.env"))

API_KEY = os.environ.get("READ_IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY")
BASE_URL = os.environ.get("READ_IMAGE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
MODEL = os.environ.get("READ_IMAGE_MODEL", "doubao-seed-2-1-turbo-260628")
VOLC_KEY = os.environ.get("VOLC_SPEECH_API_KEY") or ""
VOLC_APP_ID = os.environ.get("VOLC_SPEECH_APP_ID") or ""
VOLC_TTS_VOICE = os.environ.get("VOLC_TTS_VOICE", "zh_female_vv_uranus_bigtts")

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CTX = ssl.create_default_context()


# ---------------- 数据库 ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000).hex()


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL,
            display_name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            binding_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            to_user_id INTEGER DEFAULT NULL,
            text TEXT DEFAULT '',
            image TEXT DEFAULT '',
            steps TEXT DEFAULT '[]',
            reply TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            ts TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            note TEXT DEFAULT '',
            due TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_by INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS memos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            value TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS tutorials(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            steps TEXT DEFAULT '[]',
            source_request_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS bindings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            elder_id INTEGER NOT NULL,
            child_id INTEGER NOT NULL,
            relation TEXT DEFAULT '子女',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        """
    )
    n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if n == 0:
        salt1 = secrets.token_hex(16)
        salt2 = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO users(username,password_hash,salt,role,display_name,phone) VALUES(?,?,?,?,?,?)",
            ("laoren", hash_pw("123456", salt1.encode()), salt1, "elder", "李建国（外公）", "13800000001"),
        )
        conn.execute(
            "INSERT INTO users(username,password_hash,salt,role,display_name,phone,binding_id) VALUES(?,?,?,?,?,?,?)",
            ("zinu", hash_pw("123456", salt2.encode()), salt2, "child", "李薇（女儿）", "13800000002", 1),
        )
        conn.executemany(
            "INSERT INTO reminders(user_id,title,note,due,status,created_by) VALUES(?,?,?,?,?,?)",
            [
                (1, "吃降压药", "早饭后一粒，温水送服", "8月10日 8:30", "pending", 2),
                (1, "医院复诊", "心内科复查，带上医保卡和病历本", "8月12日 9:00", "pending", 2),
                (1, "养老金认证", "这个月 15 号前要办，别忘啦", "8月15日", "pending", 2),
                (1, "去社区领体检报告", "上周的体检报告可以拿了", "8月8日 10:00", "done", 2),
            ],
        )
        conn.execute(
            "INSERT INTO requests(user_id,text,image,steps,reply,status,ts) VALUES(?,?,?,?,?,?,?)",
            (
                1,
                "我想把朋友发的那篇文章转到家族群，找不到转发在哪",
                "",
                json.dumps(["你先点开那篇文章，看屏幕右上角", "点一下右上角那三个小圆点", "在弹出来的菜单里点『转发给朋友』，选家族群发送"], ensure_ascii=False),
                "",
                "pending",
                "2026-08-10 09:20",
            ),
        )
        conn.execute(
            "INSERT INTO requests(user_id,text,image,steps,reply,status,ts) VALUES(?,?,?,?,?,?,?)",
            (
                1,
                "手机上弹了个广告说手机中毒了，我该怎么办",
                "",
                json.dumps(["这是广告弹窗，别点任何按钮", "按手机右下角的『返回』或主屏键退出", "实在退不掉就长按电源键重启手机"], ensure_ascii=False),
                "别担心，是广告。以后遇到这种弹窗直接忽略，我教你关掉自动弹出",
                "done",
                "2026-08-09 19:20",
            ),
        )
        conn.execute(
            "INSERT INTO requests(user_id,text,image,steps,reply,status,ts) VALUES(?,?,?,?,?,?,?)",
            (
                1,
                "微信里的钱怎么转给孙女当生日红包",
                "",
                json.dumps(["你打开和孙女的聊天窗口", "点右下角的『+』，选转账", "输入金额，点转账，再按密码就行"], ensure_ascii=False),
                "对，就按 AI 说的来。输入金额后记得再核对一遍数字",
                "accepted",
                "2026-08-08 15:10",
            ),
        )
        conn.executemany(
            "INSERT INTO memos(user_id,label,value,note) VALUES(?,?,?,?)",
            [
                (1, "家里 WiFi 密码", "DianJia2026", "连不上网时用"),
                (1, "社区电话", "0571-8888-6666", "有事找社区"),
                (1, "社保卡号", "3301**********1234", "看病挂号用"),
                (1, "快递收件地址", "杭州市西湖区文三路 138 号 2 幢 502", "网购收快递用"),
            ],
        )
        salt3 = secrets.token_hex(16)
        salt4 = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO users(username,password_hash,salt,role,display_name,phone) VALUES(?,?,?,?,?,?)",
            ("laopo", hash_pw("123456", salt3.encode()), salt3, "elder", "王桂兰（外婆）", "13800000003"),
        )
        conn.execute(
            "INSERT INTO users(username,password_hash,salt,role,display_name,phone) VALUES(?,?,?,?,?,?)",
            ("sunnv", hash_pw("123456", salt4.encode()), salt4, "child", "李小雅（孙女）", "13800000004"),
        )
        waipo_id = conn.execute("SELECT id FROM users WHERE username='laopo'").fetchone()["id"]
        sunnv_id = conn.execute("SELECT id FROM users WHERE username='sunnv'").fetchone()["id"]
        conn.executemany(
            "INSERT INTO bindings(elder_id,child_id,relation) VALUES(?,?,?)",
            [
                (1, 2, "女儿"),
                (1, sunnv_id, "孙女"),
                (waipo_id, 2, "女儿"),
            ],
        )
        # 外公的求助按后辈区分：转发文章 → 孙女；其余两条 → 女儿
        conn.execute(
            "UPDATE requests SET to_user_id=? WHERE user_id=1 AND text LIKE '我想把朋友发的那篇%'",
            (sunnv_id,),
        )
        conn.execute(
            "UPDATE requests SET to_user_id=? WHERE user_id=1 AND to_user_id IS NULL",
            (2,),
        )
        # 外婆（王桂兰）的独立数据
        conn.executemany(
            "INSERT INTO reminders(user_id,title,note,due,status,created_by) VALUES(?,?,?,?,?,?)",
            [
                (waipo_id, "交水电费", "小区物业代收，微信里就能交", "8月12日", "pending", 2),
                (waipo_id, "膝盖理疗", "社区医院理疗科，每周二下午", "每周二 14:30", "pending", 2),
                (waipo_id, "社区体检", "免费体检，记得带上身份证", "8月5日 9:00", "done", 2),
            ],
        )
        conn.execute(
            "INSERT INTO requests(user_id,to_user_id,text,image,steps,reply,status,ts) VALUES(?,?,?,?,?,?,?,?)",
            (
                waipo_id,
                2,
                "手机声音太小了，怎么调大",
                "",
                json.dumps(["你先找到手机侧面的音量键", "按上面那个音量键，把声音调大", "屏幕上的小喇叭图标旁边会显示音量"], ensure_ascii=False),
                "",
                "pending",
                "2026-08-10 10:40",
            ),
        )
        conn.execute(
            "INSERT INTO requests(user_id,to_user_id,text,image,steps,reply,status,ts) VALUES(?,?,?,?,?,?,?,?)",
            (
                waipo_id,
                2,
                "怎么把照片发给老姐妹",
                "",
                json.dumps(["你打开微信，找到老姐妹的聊天窗口", "点右下角的『+』，选相册", "选好照片点发送就行"], ensure_ascii=False),
                "没错，选完照片记得点右上角的发送",
                "done",
                "2026-08-07 16:05",
            ),
        )
        conn.executemany(
            "INSERT INTO memos(user_id,label,value,note) VALUES(?,?,?,?)",
            [
                (waipo_id, "家里 WiFi 密码", "Taoyuan2026", "连不上网时用"),
                (waipo_id, "女儿电话", "13800000002", "有事直接打女儿电话"),
                (waipo_id, "社区电话", "0571-8888-7777", "有事找社区"),
            ],
        )
        conn.execute(
            "INSERT INTO tutorials(user_id,title,steps,source_request_id) VALUES(?,?,?,?)",
            (
                waipo_id,
                "把手机声音调大",
                json.dumps(["按手机侧面的音量键", "屏幕上出现小喇叭就说明调好了"], ensure_ascii=False),
                None,
            ),
        )
    conn.execute("UPDATE users SET phone=? WHERE username=?", ("13800000001", "laoren"))
    conn.execute("UPDATE users SET phone=? WHERE username=?", ("13800000002", "zinu"))
    if conn.execute("SELECT COUNT(*) c FROM tutorials").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO tutorials(user_id,title,steps,source_request_id) VALUES(?,?,?,?)",
            (
                1,
                "把微信文章转发到家族群",
                json.dumps(
                    ["你先点开微信里要转的那篇文章", "你点一下文章右上角的三个小圆点", "你在弹出的菜单里点『转发给朋友』，选家族群发送"],
                    ensure_ascii=False,
                ),
                1,
            ),
        )

    # ---------- 迁移：新列 / 绑定关系 / 演示账号 ----------
    def _add_col(table, col, ddl):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    _add_col("requests", "risk", "risk TEXT DEFAULT ''")
    _add_col("requests", "to_user_id", "to_user_id INTEGER DEFAULT NULL")
    _add_col("reminders", "paused", "paused INTEGER DEFAULT 0")
    _add_col("reminders", "repeat", "repeat TEXT DEFAULT ''")
    _add_col("reminders", "done_at", "done_at TEXT DEFAULT ''")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bindings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            elder_id INTEGER NOT NULL,
            child_id INTEGER NOT NULL,
            relation TEXT DEFAULT '子女',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        """
    )
    if conn.execute("SELECT COUNT(*) c FROM bindings").fetchone()["c"] == 0:
        old = conn.execute(
            "SELECT id, binding_id FROM users WHERE role='child' AND binding_id IS NOT NULL"
        ).fetchall()
        for r in old:
            conn.execute(
                "INSERT INTO bindings(elder_id, child_id, relation) VALUES(?,?,?)",
                (r["binding_id"], r["id"], "子女"),
            )

    def ensure_user(username, role, display_name, phone):
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row:
            return row["id"]
        salt = secrets.token_hex(16)
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,salt,role,display_name,phone) VALUES(?,?,?,?,?,?)",
            (username, hash_pw("123456", salt.encode()), salt, role, display_name, phone),
        )
        return cur.lastrowid

    def ensure_binding(elder_uid, child_uid, relation):
        if not elder_uid or not child_uid:
            return
        row = conn.execute(
            "SELECT id FROM bindings WHERE elder_id=? AND child_id=?",
            (elder_uid, child_uid),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE bindings SET relation=? WHERE id=?",
                (relation, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO bindings(elder_id, child_id, relation) VALUES(?,?,?)",
                (elder_uid, child_uid, relation),
            )

    laopo_id = ensure_user("laopo", "elder", "王桂兰（外婆）", "13800000003")
    sunnv_id = ensure_user("sunnv", "child", "李小雅（孙女）", "13800000004")
    laoren_id = conn.execute("SELECT id FROM users WHERE username='laoren'").fetchone()["id"]
    zinu_id = conn.execute("SELECT id FROM users WHERE username='zinu'").fetchone()["id"]
    ensure_binding(laoren_id, zinu_id, "女儿")
    ensure_binding(laoren_id, sunnv_id, "孙女")
    ensure_binding(laopo_id, zinu_id, "女儿")
    if not conn.execute(
        "SELECT 1 FROM reminders WHERE user_id=? AND title='买降压药'", (laopo_id,)
    ).fetchone():
        conn.execute(
            "INSERT INTO reminders(user_id,title,note,due,status,created_by) VALUES(?,?,?,?,?,?)",
            (laopo_id, "买降压药", "早饭后一片，社区药房有医保", "每天 8:00", "pending", zinu_id),
        )
    conn.commit()
    conn.close()


init_db()


# ---------------- AI 能力 ----------------

SYSTEM_PROMPT = (
    "你是'惦记'App 里的适老化手机教学助手，服务对象是 70 岁左右、不太会用智能手机的长辈。"
    "根据用户的口头描述和/或截图，判断 TA 想做什么，然后给出最多 3 步、每步一句话的大白话操作指引。"
    "要求：只说动作，不说术语；每步以'你'开头；步骤之间不要编号以外的废话；"
    "如果看不清截图就明确说'我没看清你屏幕上的字，请重新拍一张'。"
    "只输出 JSON，格式：{\"steps\":[\"第一步…\",\"第二步…\"],\"note\":\"一句话说明\"}。"
)


def call_ark(text, image_data_url=None):
    content = [{"type": "text", "text": text}]
    if image_data_url:
        m = re.match(r"data:(image/\w+);base64,(.+)", image_data_url, re.S)
        if m:
            mime, b64 = m.group(1), m.group(2)
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    payload = {
        "model": MODEL,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "max_tokens": 600,
    }
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=100, context=_CTX) as resp:
        data = json.load(resp)
    raw = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("模型未返回 JSON")
    result = json.loads(m.group(0))
    result["_ms"] = int((time.time() - t0) * 1000)
    return result


RISK_PROMPT = (
    "你是'惦记'App 的安全助手，服务对象是老年人。根据用户描述的问题和 AI 给出的指引，"
    "判断是否涉及诈骗、扣费陷阱、陌生链接、恶意软件、索要验证码/密码等风险。"
    "只输出 JSON：{\"level\":\"low\"|\"warn\"|\"danger\",\"reason\":\"一句话解释\"}。"
    "level 含义：low=正常操作；warn=需要留意（陌生链接、付费、验证码等）；danger=高度疑似诈骗或危险。"
)


def call_risk(text, steps):
    if not API_KEY:
        return {"level": "low", "reason": ""}
    try:
        prompt = (
            f"用户问题：{str(text or '')[:200]}\n"
            f"AI 给出的指引：{'；'.join([str(s) for s in (steps or [])])[:300]}"
        )
        payload = {
            "model": MODEL,
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": RISK_PROMPT},
                {"role": "user", "content": [{"type": "text", "text": prompt}]},
            ],
            "max_tokens": 150,
        }
        req = urllib.request.Request(
            BASE_URL + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=40, context=_CTX) as resp:
            data = json.load(resp)
        raw = data["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", raw, re.S)
        r = json.loads(m.group(0)) if m else {}
        level = (str(r.get("level") or "low")).strip().lower()
        if level not in ("low", "warn", "danger"):
            level = "low"
        return {"level": level, "reason": str(r.get("reason") or "")[:80]}
    except Exception:
        return {"level": "low", "reason": ""}


def asr_transcribe(b64_audio, mime_type=""):
    """火山引擎极速版语音识别：先转 16k 单声道 wav，再调用 Flash 接口。"""
    if not VOLC_KEY:
        raise ValueError("未配置 VOLC_SPEECH_API_KEY")
    with tempfile.TemporaryDirectory(prefix="dianji-asr-") as td:
        src = os.path.join(td, "in.bin")
        wav = os.path.join(td, "out.wav")
        with open(src, "wb") as f:
            f.write(base64.b64decode(b64_audio))
        if mime_type and "wav" in mime_type:
            data = open(src, "rb").read()
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-ar", "16000", "-ac", "1", "-f", "wav", wav],
                capture_output=True,
                timeout=120,
            )
            if not os.path.isfile(wav):
                raise ValueError("音频转码失败，请换一个录音方式")
            data = open(wav, "rb").read()
    body = {
        "user": {"uid": "dianji-h5"},
        "audio": {"data": base64.b64encode(data).decode("ascii")},
        "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
    }
    headers = {
        "X-Api-Resource-Id": "volc.bigasr.auc_turbo",
        "X-Api-Request-Id": uuid.uuid4().hex,
        "X-Api-Sequence": "-1",
        "Content-Type": "application/json",
    }
    if VOLC_APP_ID:
        headers["X-Api-App-Key"] = VOLC_APP_ID
        headers["X-Api-Access-Key"] = VOLC_KEY
    else:
        headers["X-Api-Key"] = VOLC_KEY
    req = urllib.request.Request(
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as resp:
        code = resp.headers.get("X-Api-Status-Code") or ""
        raw = resp.read().decode("utf-8", "ignore")
    if code != "20000000":
        raise ValueError(f"语音识别失败: {code} {raw[:200]}")
    result = json.loads(raw)
    parts = []
    r = result.get("result")
    if isinstance(r, list):
        for it in r:
            if isinstance(it, dict) and it.get("text"):
                parts.append(it["text"])
    elif isinstance(r, dict):
        if r.get("text"):
            parts.append(r["text"])
        elif isinstance(r.get("utterances"), list):
            for u in r["utterances"]:
                if u.get("text"):
                    parts.append(u["text"])
    text = "".join(parts).strip()
    if not text:
        raise ValueError("没听清，请再试一次或直接打字")
    return text


_TITLE_STOP = re.compile(r"[的了怎么把我想给到在就等是和也再都还去用要]")


def tutorial_keywords(title):
    core = _TITLE_STOP.sub(" ", title or "")
    return [w for w in core.split() if len(w) >= 2]


def find_tutorial_match(q, eid):
    if not q or not q.strip():
        return None
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tutorials WHERE user_id=? ORDER BY id DESC", (eid,)
    ).fetchall()
    conn.close()
    best = None
    for t in rows:
        d = dict(t)
        d["steps"] = json.loads(d["steps"] or "[]")
        title = d["title"] or ""
        if title and title in q:
            return d
        if best is None:
            for w in tutorial_keywords(title):
                if w in q:
                    best = d
                    break
    return best


def _extract_json_objects(text):
    objs = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, start = 0, False, False, i
        while i < len(text):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        try:
            objs.append(json.loads(text[start:i]))
        except Exception:
            pass
    return objs


def _tts_request(text):
    if not VOLC_KEY:
        raise ValueError("未配置 VOLC_SPEECH_API_KEY")
    payload = {
        "user": {"uid": "dianji-h5"},
        "req_params": {
            "text": text,
            "speaker": VOLC_TTS_VOICE,
            "audio_params": {"format": "mp3", "sample_rate": 24000, "bit_rate": 64000},
            "additions": json.dumps({"disable_markdown_filter": True, "disable_emoji_filter": True}),
        },
    }
    headers = {
        "X-Api-Resource-Id": "seed-tts-2.0",
        "X-Api-Request-Id": uuid.uuid4().hex,
        "X-Api-Sequence": "-1",
        "X-Api-Connect-Id": uuid.uuid4().hex,
        "Content-Type": "application/json",
    }
    if VOLC_APP_ID:
        headers["X-Api-App-Key"] = VOLC_APP_ID
        headers["X-Api-Access-Key"] = VOLC_KEY
    else:
        headers["X-Api-Key"] = VOLC_KEY
    return urllib.request.Request(
        "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )


def tts_synthesize(text):
    """豆包语音合成（HTTP 单向流式）：返回 (audio_bytes, mime)。"""
    text = text[:300]
    req = _tts_request(text)
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as resp:
        raw = resp.read()
    if not raw:
        raise ValueError("TTS 未返回音频")
    # 1) JSON 对象流（HTTP 接口常见）
    if (raw[0] >> 4) != 0b0001:
        pieces = []
        for j in _extract_json_objects(raw.decode("utf-8", "ignore")):
            code = j.get("code")
            if code not in (None, 0, 3000, 20000000):
                raise ValueError(f"TTS 业务错误 {code}: {j.get('message','')}")
            data = j.get("data")
            if isinstance(data, str) and data:
                pieces.append(base64.b64decode(data))
        if not pieces:
            raise ValueError("TTS 返回内容为空")
        return b"".join(pieces), "audio/mpeg"
    # 2) 二进制帧协议
    chunks, offset = [], 0
    n = len(raw)
    while offset + 8 <= n:
        msg_type = raw[offset + 1] >> 4
        flags = raw[offset + 1] & 0x0F
        cursor = offset + 4
        if flags & 0b0001:
            cursor += 4
        if cursor + 4 > n:
            break
        size = int.from_bytes(raw[cursor:cursor + 4], "big")
        cursor += 4
        if cursor + size > n:
            break
        frame = raw[cursor:cursor + size]
        if msg_type == 0b1011:
            chunks.append(frame)
        elif msg_type in (0b1001, 0b1111):
            try:
                j = json.loads(frame.decode("utf-8", "ignore"))
                code = j.get("code")
                if msg_type == 0b1111 or code not in (None, 0, 3000, 20000000):
                    raise ValueError(f"TTS 服务错误: {j.get('message','')}")
                if isinstance(j.get("data"), str) and j["data"]:
                    chunks.append(base64.b64decode(j["data"]))
            except Exception:
                pass
        offset = cursor + size
    if not chunks:
        raise ValueError("TTS 二进制帧无音频")
    return b"".join(chunks), "audio/mpeg"


def tts_synthesize_cached(text):
    """带内存缓存的语音合成：同一句话不重复请求火山接口。"""
    key = str(text or "")[:300]
    if key in _TTS_CACHE:
        try:
            _TTS_CACHE_ORDER.remove(key)
        except ValueError:
            pass
        _TTS_CACHE_ORDER.append(key)
        return _TTS_CACHE[key]
    audio, mime = tts_synthesize(key)
    _TTS_CACHE[key] = (audio, mime)
    _TTS_CACHE_ORDER.append(key)
    if len(_TTS_CACHE_ORDER) > 300:
        old = _TTS_CACHE_ORDER.pop(0)
        _TTS_CACHE.pop(old, None)
    return audio, mime


def _next_json(text, start):
    """从 start 起解析下一个完整 JSON 对象；返回 (obj, next_start) 或 (None, pos)。"""
    i = start
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, s = 0, False, False, i
        while i < len(text):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    try:
                        return json.loads(text[s:i]), i
                    except Exception:
                        return None, i
            i += 1
        return None, s
    return None, start


def stream_tts(self, text):
    """流式 TTS：边从火山生成边写回客户端（HTTP/1.0 关闭定界）。"""
    req = _tts_request(text[:300])
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as resp:
        mode = None
        buf = b""
        offset = 0
        json_text = ""
        json_pos = 0
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            if mode is None:
                mode = "bin" if (chunk[0] >> 4) == 0b0001 else "json"
            if mode == "json":
                json_text += chunk.decode("utf-8", "ignore")
                while True:
                    obj, pos = _next_json(json_text, json_pos)
                    if obj is None:
                        json_pos = pos
                        break
                    json_pos = pos
                    code = obj.get("code")
                    if code not in (None, 0, 3000, 20000000):
                        raise ValueError(f"TTS 业务错误 {code}: {obj.get('message','')}")
                    data = obj.get("data")
                    if isinstance(data, str) and data:
                        self.wfile.write(base64.b64decode(data))
                        self.wfile.flush()
            else:
                buf += chunk
                while offset + 8 <= len(buf):
                    msg_type = buf[offset + 1] >> 4
                    flags = buf[offset + 1] & 0x0F
                    cursor = offset + 4
                    if flags & 0b0001:
                        cursor += 4
                    if cursor + 4 > len(buf):
                        break
                    size = int.from_bytes(buf[cursor:cursor + 4], "big")
                    cursor += 4
                    if cursor + size > len(buf):
                        break
                    frame = buf[cursor:cursor + size]
                    if msg_type == 0b1011:
                        self.wfile.write(frame)
                        self.wfile.flush()
                    elif msg_type in (0b1001, 0b1111):
                        try:
                            j = json.loads(frame.decode("utf-8", "ignore"))
                            code = j.get("code")
                        except Exception:
                            j, code = {}, None
                        if msg_type == 0b1111 or code not in (None, 0, 3000, 20000000):
                            raise ValueError(f"TTS 服务错误: {j.get('message','')}")
                        if isinstance(j.get("data"), str) and j["data"]:
                            self.wfile.write(base64.b64decode(j["data"]))
                            self.wfile.flush()
                    offset = cursor + size


# ---------------- 鉴权 ----------------

def get_user_by_token(token):
    if not token:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
        (token,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def user_json(u):
    return {
        "id": u["id"],
        "username": u["username"],
        "role": u["role"],
        "display_name": u["display_name"],
        "phone": u["phone"],
        "binding_id": u["binding_id"],
    }


def enrich_reminder(r):
    """把自由文本 due 解析成结构化字段，供前端做“今天/已过点”提醒。"""
    due = (r.get("due") or "").strip()
    now = datetime.datetime.now()
    today = now.date()
    out = {
        "due_type": "once",
        "due_hhmm": "",
        "due_date": "",
        "is_today": False,
        "is_overdue": False,
    }
    if not due:
        return out
    m = re.search(r"(\d{1,2})[:：](\d{2})", due)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            out["due_hhmm"] = "%02d:%02d" % (hh, mm)
    date_s = ""
    m = re.search(r"(\d{1,2})月(\d{1,2})日", due)
    if m:
        try:
            date_s = datetime.date(today.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            date_s = ""
    if not date_s:
        m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", due)
        if m:
            try:
                date_s = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                date_s = ""
    if not date_s:
        m = re.search(r"(\d{1,2})[-/](\d{1,2})", due)
        if m:
            try:
                date_s = datetime.date(today.year, int(m.group(1)), int(m.group(2))).isoformat()
            except ValueError:
                date_s = ""
    out["due_date"] = date_s
    if "每天" in due or "每日" in due or "天天" in due or re.match(r"^\s*\d{1,2}[:：]\d{2}", due):
        out["due_type"] = "daily"
    elif "每周" in due or "星期" in due or re.search(r"周[一二三四五六日天]", due):
        out["due_type"] = "weekly"
    if out["due_type"] == "once" and date_s:
        out["is_today"] = date_s == today.isoformat()
        out["is_overdue"] = date_s < today.isoformat()
    elif out["due_type"] == "daily":
        out["is_today"] = True
        if out["due_hhmm"] and out["due_hhmm"] < now.strftime("%H:%M"):
            out["is_overdue"] = True
    return out


def save_image(b64):
    """把 base64 图片落盘，返回可访问的 /uploads/ 相对路径；失败返回空串。"""
    if not b64 or not isinstance(b64, str) or not b64.startswith("data:image"):
        return ""
    m = re.match(r"data:(image/\w+);base64,(.+)", b64, re.S)
    if not m:
        return ""
    mime, data = m.group(1), base64.b64decode(m.group(2))
    if not data or len(data) > 8 * 1024 * 1024:
        return ""
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
    name = f"req_{secrets.token_hex(8)}.{ext}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    return "/uploads/" + name


def parse_risk(raw):
    """把 requests.risk 字段解析成 dict；空或非法返回 low。"""
    if not raw:
        return {"level": "low", "reason": ""}
    try:
        r = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {"level": "low", "reason": ""}
    level = str(r.get("level") or "low").strip().lower()
    if level not in ("low", "warn", "danger"):
        level = "low"
    return {"level": level, "reason": str(r.get("reason") or "")[:80]}


# ---------------- HTTP 服务 ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _sec_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(self), microphone=(self)")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'self'; "
            "base-uri 'self'; form-action 'self'",
        )

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit=60 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _auth(self):
        h = self.headers.get("Authorization") or ""
        token = h[7:] if h.startswith("Bearer ") else ""
        user = get_user_by_token(token)
        if not user:
            self._json(401, {"error": "请先登录"})
            return None
        return user

    def _bound_elder_ids(self, user):
        if user["role"] == "elder":
            return [user["id"]]
        conn = get_db()
        rows = conn.execute(
            "SELECT elder_id FROM bindings WHERE child_id=?", (user["id"],)
        ).fetchall()
        conn.close()
        return [r["elder_id"] for r in rows]

    def _target_elder_id(self, user, requested=None):
        """子女端取请求指定的长辈（必须在绑定内），否则取第一个绑定长辈。"""
        ids = self._bound_elder_ids(user)
        if not ids:
            return None
        if requested in ids:
            return requested
        return ids[0]

    def _require_child(self, user):
        if user["role"] != "child":
            self._json(403, {"error": "该操作仅子女端可用"})
            return False
        return True

    def _require_elder(self, user):
        if user["role"] != "elder":
            self._json(403, {"error": "该操作仅老人端可用"})
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self._sec_headers()
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        query = urlparse(self.path).query
        qs = parse_qs(query)
        if path == "/api/me":
            user = self._auth()
            if user:
                self._json(200, {"user": user_json(user)})
            return
        if path == "/api/requests":
            user = self._auth()
            if not user:
                return
            eid = self._target_elder_id(user, int((qs.get("elder_id") or [0])[0]) or None)
            if not eid:
                self._json(200, [])
                return
            conn = get_db()
            if user["role"] == "elder":
                rows = conn.execute(
                    "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC", (eid,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM requests WHERE user_id=? AND (to_user_id IS NULL OR to_user_id=?) ORDER BY id DESC",
                    (eid, user["id"]),
                ).fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                d["steps"] = json.loads(d["steps"] or "[]")
                d["risk"] = parse_risk(d.get("risk"))
                out.append(d)
            self._json(200, out)
            return
        if path == "/api/reminders":
            user = self._auth()
            if not user:
                return
            eid = self._target_elder_id(user, int((qs.get("elder_id") or [0])[0]) or None)
            if not eid:
                self._json(200, [])
                return
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM reminders WHERE user_id=? ORDER BY status='done', id", (eid,)
            ).fetchall()
            out = []
            today = datetime.date.today().isoformat()
            week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
            for r in rows:
                d = dict(r)
                if d.get("repeat") == "daily" and d.get("status") == "done" and (d.get("done_at") or "")[:10] < today:
                    conn.execute("UPDATE reminders SET status='pending', done_at='' WHERE id=?", (d["id"],))
                    d["status"] = "pending"
                    d["done_at"] = ""
                elif d.get("repeat") == "weekly" and d.get("status") == "done" and (d.get("done_at") or "")[:10] < week_ago:
                    conn.execute("UPDATE reminders SET status='pending', done_at='' WHERE id=?", (d["id"],))
                    d["status"] = "pending"
                    d["done_at"] = ""
                d.update(enrich_reminder(d))
                out.append(d)
            conn.commit()
            conn.close()
            self._json(200, out)
            return
        if path == "/api/memos":
            user = self._auth()
            if not user:
                return
            eid = self._target_elder_id(user, int((qs.get("elder_id") or [0])[0]) or None)
            if not eid:
                self._json(200, [])
                return
            conn = get_db()
            rows = conn.execute("SELECT * FROM memos WHERE user_id=? ORDER BY id", (eid,)).fetchall()
            conn.close()
            self._json(200, [dict(r) for r in rows])
            return
        if path == "/api/family":
            user = self._auth()
            if not user:
                return
            conn = get_db()
            if user["role"] == "elder":
                rows = conn.execute(
                    "SELECT u.id,u.display_name,u.phone,b.relation FROM bindings b "
                    "JOIN users u ON u.id=b.child_id WHERE b.elder_id=?",
                    (user["id"],),
                ).fetchall()
                conn.close()
                self._json(200, {"children": [dict(r) for r in rows]})
                return
            rows = conn.execute(
                "SELECT u.id,u.display_name,u.phone,b.relation FROM bindings b "
                "JOIN users u ON u.id=b.elder_id WHERE b.child_id=?",
                (user["id"],),
            ).fetchall()
            conn.close()
            self._json(200, {"elders": [dict(r) for r in rows]})
            return
        if path == "/api/tutorials":
            user = self._auth()
            if not user:
                return
            eid = self._target_elder_id(user, int((qs.get("elder_id") or [0])[0]) or None)
            if not eid:
                self._json(200, [])
                return
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM tutorials WHERE user_id=? ORDER BY id DESC", (eid,)
            ).fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                d["steps"] = json.loads(d["steps"] or "[]")
                out.append(d)
            self._json(200, out)
            return
        if path == "/api/tutorials/match":
            user = self._auth()
            if not user:
                return
            q = (parse_qs(query).get("q") or [""])[0]
            eid = self._target_elder_id(user, int((qs.get("elder_id") or [0])[0]) or None)
            if not eid:
                self._json(200, {"match": None})
                return
            self._json(200, {"match": find_tutorial_match(q, eid)})
            return
        if path == "/api/tts":
            qs = parse_qs(query)
            user = get_user_by_token((qs.get("token") or [""])[0])
            if not user:
                self._json(401, {"error": "请先登录"})
                return
            text = (qs.get("text") or [""])[0] or ""
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                stream_tts(self, text)
            except Exception:
                pass
            return
        if path.startswith("/uploads/"):
            name = os.path.basename(path)
            full = os.path.join(UPLOAD_DIR, name)
            if not os.path.isfile(full):
                self._json(404, {"error": "not found"})
                return
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            body = open(full, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self._sec_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", ""):
            path = "/h5/index.html"
        full = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
        if not full.startswith(ROOT):
            self._json(403, {"error": "forbidden"})
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json",):
            ctype += "; charset=utf-8"
        body = open(full, "rb").read()
        etag = '"%x-%x"' % (len(body), int(os.path.getmtime(full)))
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self._sec_headers()
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self._sec_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/login":
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            conn = get_db()
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not row or hash_pw(password, row["salt"].encode()) != row["password_hash"]:
                conn.close()
                self._json(401, {"error": "账号或密码不对"})
                return
            token = secrets.token_hex(24)
            conn.execute("INSERT INTO sessions(token,user_id) VALUES(?,?)", (token, row["id"]))
            conn.commit()
            conn.close()
            self._json(200, {"token": token, "user": user_json(row)})
            return

        if path == "/api/logout":
            h = self.headers.get("Authorization") or ""
            token = h[7:] if h.startswith("Bearer ") else ""
            if token:
                conn = get_db()
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                conn.commit()
                conn.close()
            self._json(200, {"ok": True})
            return

        user = self._auth()
        if not user:
            return

        if path == "/api/help":
            if not API_KEY:
                self._json(500, {"error": "未配置 API 密钥"})
                return
            try:
                result = call_ark(body.get("text") or "请帮我看一下这个界面", body.get("image"))
                result["risk"] = call_risk(body.get("text") or "", result.get("steps") or [])
                self._json(200, result)
            except Exception as e:
                self._json(502, {"error": str(e)[:300]})
            return

        if path == "/api/help/followup":
            if not API_KEY:
                self._json(500, {"error": "未配置 API 密钥"})
                return
            try:
                question = body.get("question") or ""
                step = body.get("step") or ""
                idx = int(body.get("idx") or 0)
                text = (
                    f"之前的问题是：{question}。我做到第 {idx + 1} 步：{step}，"
                    "这一步没看懂，请只把这一步再拆成最多 3 个更细的小步骤，只说这一步。"
                )
                result = call_ark(text, body.get("image"))
                self._json(200, result)
            except Exception as e:
                self._json(502, {"error": str(e)[:300]})
            return

        if path == "/api/asr":
            try:
                text = asr_transcribe(body.get("audio") or "", body.get("mime") or "")
                self._json(200, {"text": text})
            except Exception as e:
                self._json(502, {"error": str(e)[:300]})
            return

        if path == "/api/forward":
            if not self._require_elder(user):
                return
            eid = user["id"]
            image_url = save_image(body.get("image") or "")
            risk_raw = json.dumps(body.get("risk") or {"level": "low", "reason": ""}, ensure_ascii=False)
            conn = get_db()
            to_user_id = body.get("to_user_id")
            if to_user_id is not None:
                ok_target = conn.execute(
                    "SELECT 1 FROM bindings WHERE elder_id=? AND child_id=?",
                    (eid, to_user_id),
                ).fetchone()
                if not ok_target:
                    to_user_id = None
            cur = conn.execute(
                "INSERT INTO requests(user_id,to_user_id,text,image,steps,status,ts,risk) VALUES(?,?,?,?,?,?,?,?)",
                (
                    eid,
                    to_user_id,
                    body.get("text", ""),
                    image_url,
                    json.dumps(body.get("steps", []), ensure_ascii=False),
                    "pending",
                    time.strftime("%Y-%m-%d %H:%M"),
                    risk_raw,
                ),
            )
            conn.commit()
            rid = cur.lastrowid
            conn.close()
            self._json(200, {"id": rid})
            return

        if path == "/api/tts":
            try:
                audio, mime = tts_synthesize_cached(body.get("text") or "")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self._sec_headers()
                self.end_headers()
                self.wfile.write(audio)
            except Exception as e:
                self._json(502, {"error": str(e)[:300]})
            return

        if path == "/api/reply":
            if not self._require_child(user):
                return
            rid = body.get("id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            conn = get_db()
            cur = conn.execute(
                "UPDATE requests SET reply=?, status='accepted' WHERE id=? AND user_id=?",
                (body.get("reply", ""), rid, eid),
            )
            conn.commit()
            ok = cur.rowcount > 0
            conn.close()
            if not ok:
                self._json(404, {"error": "求助不存在"})
                return
            self._json(200, {"ok": True})
            return

        if path == "/api/status":
            if not self._require_child(user):
                return
            rid = body.get("id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            status = body.get("status", "pending")
            if status not in ("pending", "accepted", "done", "withdrawn"):
                status = "pending"
            conn = get_db()
            cur = conn.execute(
                "UPDATE requests SET status=? WHERE id=? AND user_id=?",
                (status, rid, eid),
            )
            conn.commit()
            ok = cur.rowcount > 0
            conn.close()
            if not ok:
                self._json(404, {"error": "求助不存在"})
                return
            self._json(200, {"ok": True})
            return

        if path == "/api/requests/withdraw":
            if not self._require_elder(user):
                return
            rid = body.get("id")
            conn = get_db()
            conn.execute(
                "UPDATE requests SET status='withdrawn' WHERE id=? AND user_id=? AND status IN ('pending','accepted')",
                (rid, user["id"]),
            )
            conn.commit()
            conn.close()
            self._json(200, {"ok": True})
            return

        if path == "/api/reminders":
            if not self._require_child(user):
                return
            eid = self._target_elder_id(user, body.get("elder_id"))
            if not eid:
                self._json(404, {"error": "未绑定长辈"})
                return
            repeat = enrich_reminder({"due": body.get("due", "")})["due_type"]
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO reminders(user_id,title,note,due,status,created_by,repeat) VALUES(?,?,?,?,?,?,?)",
                (eid, body.get("title", "待办"), body.get("note", ""), body.get("due", ""), "pending", user["id"], repeat),
            )
            conn.commit()
            rid = cur.lastrowid
            conn.close()
            self._json(200, {"id": rid})
            return

        if path == "/api/reminders/done":
            if not self._require_elder(user):
                return
            rid = body.get("id")
            conn = get_db()
            conn.execute(
                "UPDATE reminders SET status='done', done_at=? WHERE id=? AND user_id=?",
                (time.strftime("%Y-%m-%d %H:%M"), rid, user["id"]),
            )
            conn.commit()
            conn.close()
            self._json(200, {"ok": True})
            return

        if path == "/api/reminders/delete":
            if not self._require_child(user):
                return
            rid = body.get("id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            conn = get_db()
            conn.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (rid, eid))
            conn.commit()
            conn.close()
            self._json(200, {"ok": True})
            return

        if path == "/api/reminders/update":
            if not self._require_child(user):
                return
            rid = body.get("id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            repeat = enrich_reminder({"due": body.get("due", "")})["due_type"]
            conn = get_db()
            cur = conn.execute(
                "UPDATE reminders SET title=?, note=?, due=?, repeat=? WHERE id=? AND user_id=?",
                (body.get("title", "待办"), body.get("note", ""), body.get("due", ""), repeat, rid, eid),
            )
            conn.commit()
            ok = cur.rowcount > 0
            conn.close()
            self._json(200, {"ok": ok})
            return

        if path == "/api/reminders/pause":
            if not self._require_child(user):
                return
            rid = body.get("id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            paused = 1 if body.get("paused") else 0
            conn = get_db()
            cur = conn.execute(
                "UPDATE reminders SET paused=? WHERE id=? AND user_id=?",
                (paused, rid, eid),
            )
            conn.commit()
            ok = cur.rowcount > 0
            conn.close()
            self._json(200, {"ok": ok})
            return

        if path == "/api/memos":
            if not self._require_child(user):
                return
            eid = self._target_elder_id(user, body.get("elder_id"))
            if not eid:
                self._json(404, {"error": "未绑定长辈"})
                return
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO memos(user_id,label,value,note) VALUES(?,?,?,?)",
                (eid, body.get("label", ""), body.get("value", ""), body.get("note", "")),
            )
            conn.commit()
            rid = cur.lastrowid
            conn.close()
            self._json(200, {"id": rid})
            return

        if path == "/api/memos/delete":
            if not self._require_child(user):
                return
            rid = body.get("id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            conn = get_db()
            conn.execute("DELETE FROM memos WHERE id=? AND user_id=?", (rid, eid))
            conn.commit()
            conn.close()
            self._json(200, {"ok": True})
            return

        if path == "/api/tutorials":
            if not self._require_child(user):
                return
            eid = self._target_elder_id(user, body.get("elder_id"))
            if not eid:
                self._json(404, {"error": "未绑定长辈"})
                return
            steps = body.get("steps")
            if not isinstance(steps, list):
                steps = [s.strip() for s in str(steps or "").splitlines() if s.strip()]
            conn = get_db()
            cur = conn.execute(
                "INSERT INTO tutorials(user_id,title,steps,source_request_id) VALUES(?,?,?,?)",
                (eid, body.get("title", "教程"), json.dumps(steps, ensure_ascii=False), None),
            )
            conn.commit()
            rid = cur.lastrowid
            conn.close()
            self._json(200, {"id": rid})
            return

        if path == "/api/tutorials/delete":
            if not self._require_child(user):
                return
            rid = body.get("id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            conn = get_db()
            conn.execute("DELETE FROM tutorials WHERE id=? AND user_id=?", (rid, eid))
            conn.commit()
            conn.close()
            self._json(200, {"ok": True})
            return

        if path == "/api/tutorials/from-request":
            if not self._require_child(user):
                return
            rid = body.get("request_id")
            eid = self._target_elder_id(user, body.get("elder_id"))
            conn = get_db()
            row = conn.execute(
                "SELECT * FROM requests WHERE id=? AND user_id=?", (rid, eid)
            ).fetchone()
            if not row:
                conn.close()
                self._json(404, {"error": "求助不存在"})
                return
            title = (row["text"] or "求助教程")[:40]
            cur = conn.execute(
                "INSERT INTO tutorials(user_id,title,steps,source_request_id) VALUES(?,?,?,?)",
                (eid, title, row["steps"], rid),
            )
            conn.commit()
            conn.close()
            self._json(200, {"id": cur.lastrowid})
            return

        if path == "/api/profile":
            name = (body.get("display_name") or "").strip()
            phone = (body.get("phone") or "").strip()
            if not name:
                self._json(400, {"error": "昵称不能为空"})
                return
            conn = get_db()
            conn.execute(
                "UPDATE users SET display_name=?, phone=? WHERE id=?",
                (name, phone, user["id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            conn.close()
            self._json(200, {"user": user_json(row)})
            return

        self._json(404, {"error": "unknown api"})


def main():
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"惦记 H5 服务已启动: http://localhost:{port}/h5/  (DB: {DB_PATH})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
