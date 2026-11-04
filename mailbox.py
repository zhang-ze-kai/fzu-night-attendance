#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮箱双向通道：系统发请求邮件 → 用户直接回复 → 系统读回复。

这是 AI 与用户之间的远程控制通道：任何脚本无法自主解决的问题（短信验证码、
AI 需要的补充信息、人工决策），都可以通过"请求邮件 → 用户回复 → 读取回复"
的闭环解决，无需用户打开电脑。

配置来源：notify.json 的 notify_key（smtp主机|端口|发件邮箱|授权码|收件邮箱），
IMAP 主机自动从发件邮箱域名推导（imap.<域名>:993）。
安全：只接受发件人=自己且主题含本次请求标记（ASCII marker）的回复。
"""
import email
import imaplib
import json
import re
import time
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formatdate, parseaddr
from pathlib import Path

import requests

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def load_notify(base_dir=None):
    """读通知配置并解析成结构化 dict。"""
    here = Path(__file__).resolve().parent
    for cand in [Path("notify.json")] + ([Path(base_dir) / "notify.json"] if base_dir else []):
        try:
            j = json.loads(Path(cand).read_text(encoding="utf-8"))
        except Exception:
            continue
        nk = j.get("notify_key", "")
        parts = nk.split("|")
        if j.get("notify_channel") == "email" and len(parts) >= 5:
            user = parts[2].strip()
            domain = user.split("@")[-1]
            return {
                "smtp_host": parts[0].strip(), "smtp_port": int(parts[1]),
                "user": user, "code": parts[3].strip(),
                "to": [x.strip() for x in parts[4].split(",")],
                "imap_host": "imap." + domain, "imap_port": 993,
            }
    return None


def _decode(s):
    out = ""
    for t, enc in decode_header(s or ""):
        out += t.decode(enc or "utf-8", "replace") if isinstance(t, bytes) else t
    return out


def send(cfg, subject, body, message_id=None):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = ", ".join(cfg["to"])
    msg["Date"] = formatdate(localtime=True)
    if message_id:
        msg["Message-ID"] = message_id
    smtp = __import__("smtplib").SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=25)
    try:
        smtp.login(cfg["user"], cfg["code"])
        smtp.sendmail(cfg["user"], cfg["to"], msg.as_string())
        return True, ""
    finally:
        smtp.quit()


def _get_text(msg):
    """提取 text/plain 正文（递归 multipart）。"""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    cs = part.get_content_charset() or "utf-8"
                    return payload.decode(cs, "replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def _first_answer(body, prompt_text):
    """从回复正文提取用户手打的首行答复（剔除引用的原请求行/引用符/空行）。"""
    prompt_lines = set(l.strip() for l in (prompt_text or "").splitlines() if l.strip())
    skip_kw = ("请直接回复本邮件", "写所需内容", "第一行写")
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s in prompt_lines or any(k in s for k in skip_kw):
            continue
        if s.startswith(">") or s.startswith("----") or s.lower().startswith("于 "):
            continue
        return s
    return ""


def fetch_replies(cfg, marker, since_ts, exclude_msgid=None, prompt_text=None,
                  mark_seen=True):
    """IMAP 拉取：发件人=自己、时间在 since_ts 之后、非请求原件、主题/正文含 marker。

    返回 list of {"subject", "answer", "date"}（按时间升序），answer 为用户手打的首行。
    """
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
    try:
        imap.login(cfg["user"], cfg["code"])
        imap.select("INBOX")
        import datetime
        dt = datetime.datetime.fromtimestamp(since_ts)
        since = "%02d-%s-%04d" % (dt.day, MONTHS[dt.month - 1], dt.year)
        typ, data = imap.search(None, f'(FROM "{cfg["user"]}" SINCE "{since}")')
        ids = (data[0] or b"").split()
        out = []
        for eid in reversed(ids):  # 新的在前
            typ, md = imap.fetch(eid, "(RFC822)")
            raw = md[0][1]
            msg = email.message_from_bytes(raw)
            subject = _decode(msg.get("Subject", ""))
            if marker not in subject and marker not in _get_text(msg):
                continue
            if exclude_msgid and (msg.get("Message-ID", "") or "").strip() == exclude_msgid:
                continue  # 跳过我们自己发的请求原件
            ts = None
            try:
                d = email.utils.parsedate_to_datetime(msg.get("Date"))
                ts = d.timestamp()
            except Exception:
                ts = time.time()
            body = _get_text(msg).strip()
            answer = _first_answer(body, prompt_text)
            out.append({"subject": subject, "answer": answer, "body": body,
                        "ts": ts or 0, "id": eid.decode()})
            if mark_seen:
                imap.store(eid, "+FLAGS", "\\Seen")
        return sorted(out, key=lambda x: x["ts"])
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def request_and_wait(cfg, marker, title, body, timeout_s=1500, poll_s=30,
                     deadline_ts=None):
    """发请求邮件（带自识别 Message-ID）并轮询等待用户手打的答复。

    返回 {"answer": str|None, "elapsed": 秒}。deadline_ts（epoch 秒）存在时
    等待时间自动收窄，绝不超过它。
    """
    msgid = f"<{marker}@fdu-mail>"
    send(cfg, f"[{marker}] {title}", body, message_id=msgid)
    start = time.time()
    end = start + timeout_s
    if deadline_ts:
        end = min(end, deadline_ts)
    while time.time() < end:
        time.sleep(poll_s)
        replies = fetch_replies(cfg, marker, start - 60,
                                exclude_msgid=msgid, prompt_text=body)
        replies = [r for r in replies if r.get("answer")]
        if replies:
            return {"answer": replies[-1]["answer"], "elapsed": round(time.time() - start)}
    return {"answer": None, "elapsed": round(time.time() - start)}
