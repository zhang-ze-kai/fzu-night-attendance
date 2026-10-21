#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多渠道消息推送：签到成功/失败、AI 无法解决的问题都推给账号主人。

配置（notify.json 或账号 config.json）：
  notify_channel: "serverchan" | "pushplus" | "wecom"
  notify_key:     Server酱 SendKey / PushPlus token / 企业微信机器人 webhook URL
"""

import json
from pathlib import Path

import requests


def load_notify_config(base_dir=None, account_cfg=None):
    """优先级：账号 config > 根目录 notify.json。"""
    if account_cfg and account_cfg.get("notify_channel"):
        return account_cfg.get("notify_channel"), account_cfg.get("notify_key")
    for cand in [Path("notify.json")] + ([Path(base_dir) / "notify.json"] if base_dir else []):
        try:
            j = json.loads(Path(cand).read_text(encoding="utf-8"))
            if j.get("notify_channel") and j.get("notify_key"):
                return j["notify_channel"], j["notify_key"]
        except Exception:
            continue
    return None, None


def push(title, content, channel=None, key=None):
    """发送通知。任何异常都不抛出（推送失败不影响签到）。返回描述文本。"""
    try:
        if not channel or not key:
            return "（未配置通知渠道，消息仅写日志）"
        content = (content or "")[:1800]

        if channel == "serverchan":
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": title[:32], "desp": content}, timeout=15)
        elif channel == "pushplus":
            r = requests.post("https://www.pushplus.plus/send", timeout=15, json={
                "token": key, "title": title[:100],
                "content": content, "template": "txt"})
        elif channel == "wecom":
            r = requests.post(key, timeout=15, json={
                "msgtype": "text",
                "text": {"content": f"{title}\n{content}"[:2000]}})
        elif channel == "email":
            return _send_email(title, content, key)
        else:
            return f"（未知通知渠道 {channel}）"

        ok = r.status_code == 200
        detail = r.text[:120]
        return f"推送{'成功' if ok else '失败'}: {detail}"
    except Exception as e:
        return f"推送异常: {type(e).__name__}: {e}"


def _send_email(title, content, key):
    """key 格式: smtp_host|smtp_port|发件邮箱|授权码|收件邮箱（竖线分隔，收件可为多个逗号分隔）"""
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import parseaddr

    parts = key.split("|")
    if len(parts) < 5:
        return "邮件配置不完整（应为 smtp_host|smtp_port|发件邮箱|授权码|收件邮箱）"
    host, port, user, code, to = parts[0], int(parts[1]), parts[2], parts[3], parts[4]

    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = parseaddr(f"晚点名签到助手 <{user}>")[1]
    msg["To"] = parseaddr(to.split(",")[0])[1]

    smtp = smtplib.SMTP_SSL(host, port, timeout=20)
    try:
        smtp.login(user, code)
        smtp.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
        return "邮件发送成功"
    finally:
        smtp.quit()
