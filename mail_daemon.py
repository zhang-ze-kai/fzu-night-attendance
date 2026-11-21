#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮箱对话管家：用户随时发邮件提问 → AI 读邮件 + 查系统实时状态 → 邮件回复。

由 cron 每分钟调用（/etc/cron.d/fdu-maildaemon），用 flock 防重叠。
只处理：发件人=自己、非已处理、非系统流程邮件（排除 FDU-RQ-/FDU-AI-/FDU-TEST- 标记，
那些是 bootstrap/主流程的交互邮件，由各自的轮询负责）。
"""
import email
import imaplib
import json
import re
import sys
import time
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mailbox import _decode, _get_text, load_notify

SKIP_MARKERS = ("FDU-RQ-", "FDU-AI-", "FDU-TEST-", "FDU-DAEMON", "FDU-")
SYSTEM_SUBJECT_PREFIX = ("晚点名签到", "晚点名签到异常")  # 系统通知邮件的主题前缀，永不当作提问
STATE = HERE / "mail_daemon_state.json"
API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-5.3-flash"


def load_processed():
    try:
        return set(json.loads(STATE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_processed(ids):
    # 只保留 7 天内的记录，防止无限增长
    keep = ids[-500:]
    STATE.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def _key():
    n = load_notify(str(HERE))
    if not n:
        sys.exit("notify.json 未配置")
    return n


def _glm(messages, timeout=90, max_tokens=1024):
    import requests
    key = _load_key()
    r = requests.post(API_URL, timeout=timeout, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "max_tokens": max_tokens})
    j = r.json()
    msg = j["choices"][0].get("message", {})
    out = (msg.get("content") or "").strip()
    if not out:
        out = (msg.get("reasoning_content") or "").strip()[-800:]
    return out


def _load_key(api_key=None):
    if api_key:
        return api_key
    try:
        cfg = json.loads((HERE / "accounts" / "832404228" / "config.json").read_text(encoding="utf-8"))
        return cfg.get("glm_api_key")
    except Exception:
        return None


def _live_status():
    """两个账号的实时签到状态（今天的 statusData）。"""
    from checkin import AttnClient
    out = []
    acc_dir = HERE / "accounts"
    for d in sorted(acc_dir.iterdir()):
        f = d / "config.json"
        if not f.exists():
            continue
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
            c = AttnClient(cfg.get("token"), cfg.get("base_url"), user_agent=cfg.get("user_agent"))
            data = c.init()
            dd = (data.get("data") or data)
            sd = dd.get("statusData")
            u = dd.get("userData") or {}
            if sd and sd.get("status"):
                out.append(f"{d.name}: 今日已签到 {sd.get('status')} at "
                           f"{json.loads(sd['attnData']).get('checkInTime', '?') if sd.get('attnData') else '?'}"
                           + ("（迟到）" if json.loads(sd['attnData']).get('isLate') == '1' else ""))
            elif u.get("FPlanID"):
                out.append(f"{d.name}: 今日有考勤计划，尚未签到")
            else:
                out.append(f"{d.name}: 今日暂无考勤计划")
        except Exception as e:
            out.append(f"{d.name}: 查询失败 {str(e)[:60]}")
    return out


def _recent_log(lines=40):
    rl = HERE / "run.log"
    if rl.exists():
        return "\n".join(rl.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    return "（run.log 为空）"


def _answer(user_msg):
    """组装上下文交给 GLM 回答。"""
    import datetime
    now = datetime.datetime.now()
    ctx = {
        "当前时间": now.strftime("%Y-%m-%d %H:%M:%S"),
        "签到窗口": "每晚 21:00-23:59（cron 21:10 自动运行）",
        "实时状态": _live_status(),
        "最近日志": _recent_log(30),
    }
    prompt = ("用户通过邮件向你提问：\n" + user_msg[:800]
              + "\n\n系统实时上下文（JSON）：\n" + json.dumps(ctx, ensure_ascii=False)[:3000]
              + "\n\n请直接回答用户的问题（200 字内）。如果用户要求执行操作（如重跑、修改），"
                "说明你需要维护者介入，不要假装已执行。")
    return _glm([
        {"role": "system", "content":
            "你是福大晚点名自动签到系统的邮箱管家。用户是系统主人，通过邮件与你对话。"
            "你只能基于提供的实时上下文回答和解释；不能执行除查询外的任何操作。"
            "回答简洁、诚实；不确定就说不确定。"},
        {"role": "user", "content": prompt},
    ])


def _send_reply(cfg, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = cfg["user"]
    import smtplib
    smtp = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=25)
    try:
        smtp.login(cfg["user"], cfg["code"])
        smtp.sendmail(cfg["user"], cfg["to"], msg.as_string())
    finally:
        smtp.quit()


def main():
    import fcntl
    lock = open("/tmp/fdu_maildaemon.lock", "w")
    # 每日回复上限与最小间隔（防刷屏兜底）
    replies_today = 0
    last_reply_ts = 0.0
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        sys.exit(0)  # 上一次轮询还没结束，跳过本轮
    cfg = _key()
    processed = load_processed()

    imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
    imap.login(cfg["user"], cfg["code"])
    imap.select("INBOX")
    import datetime
    dt = datetime.datetime.now() - datetime.timedelta(days=3)
    since = "%02d-%s-%04d" % (dt.day, ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][dt.month-1], dt.year)
    # 不加 UNSEEN——RFC822 抓取会自动标读，UNSEEN 条件会让邮件永远丢失；去重靠 processed 集合
    typ, data = imap.search(None, f'(FROM "{cfg["user"]}" SINCE "{since}")')
    ids = (data[0] or b"").split()
    new_count = 0
    for eid in reversed(ids):
        typ, md = imap.fetch(eid, "(BODY.PEEK[])")  # PEEK：不自动标读
        raw = md[0][1]
        msg = email.message_from_bytes(raw)
        msgid = (msg.get("Message-ID", "") or "").strip()
        subject = _decode(msg.get("Subject", ""))
        if msgid in processed or any(m in subject for m in SKIP_MARKERS):
            continue
        if subject.startswith(SYSTEM_SUBJECT_PREFIX) or "签到异常" in subject:
            processed.add(msgid)  # 系统通知邮件：记为已处理但不回复
            continue
        body = _get_text(msg).strip()
        # 用户的手打内容：剔除引用与系统提示行
        user_msg = ""
        for line in body.splitlines():
            s = line.strip()
            if not s or s.startswith(">") or s.startswith("----"):
                continue
            if any(k in s for k in ("请直接回复", "写所需内容", "第一行写", "FDU-RQ", "FDU-AI")):
                continue
            user_msg += s + "\n"
        user_msg = user_msg.strip()[:800]
        if not user_msg:
            processed.add(msgid)
            continue

        new_count += 1
        print(f"[daemon] 处理用户邮件: {subject[:60]}")
        try:
            answer = _answer(user_msg)
        except Exception as e:
            answer = f"管家处理出错: {e}"
        reply_body = (f"你的提问：\n{user_msg}\n\n管家回复：\n{answer}\n\n"
                      f"（系统时间 {time.strftime('%m-%d %H:%M')}）")
        import datetime
        today = time.strftime("%Y%m%d")
        if replies_today >= 30 or (last_reply_ts and time.time() - last_reply_ts < 90):
            print(f"[daemon] 达到每日上限或间隔不足，跳过回复（记为已处理）")
            processed.add(msgid)
            continue
        try:
            _send_reply(cfg, f"[FDU-DAEMON] Re: {subject[:70]}", reply_body)
            print(f"[daemon] 已回复")
            replies_today += 1
            last_reply_ts = time.time()
        except Exception as e:
            print(f"[daemon] 回复发送失败: {e}")
        processed.add(msgid)
        save_processed(list(processed))  # 每封即落盘，崩溃不再重刷

    imap.logout()
    save_processed(list(processed))
    if new_count:
        print(f"[daemon] 共处理 {new_count} 封")


if __name__ == "__main__":
    main()
