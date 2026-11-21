#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮箱对话管家：用户随时发邮件提问 → AI 读邮件 + 查系统实时状态 → 邮件回复。

由 cron 每分钟调用（/etc/cron.d/fdu-maildaemon），flock 防重叠。

自识别策略（重要教训：用户回复管家的邮件，主题会继承 Re:[FDU-DAEMON] 前缀，
不能按主题子串跳过，否则用户的回复会被当成管家自己的邮件永久忽略）：
  - 管家发出的每封回复带 Message-ID <FDU-DREP-...@fdu-mail>
  - 扫描时 msgid 含 FDU-DREP，或主题以 [FDU-DAEMON] 开头 = 管家出站 → 跳过
  - FDU-RQ-/FDU-AI-/FDU-TEST- 请求邮件由 bootstrap/main 各自的轮询消费，管家不碰

QQ 邮箱 IMAP 实测忽略 SINCE 条件（恒返全部）→ 两段式抓取：
先只抓头部（MESSAGE-ID/SUBJECT/DATE）分拣，候选邮件才抓全文；客户端按日期过滤。
"""
import email
import imaplib
import json
import sys
import time
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from mailbox import _decode, _get_text, load_notify

SKIP_SUBJECT_SUBSTR = ("FDU-RQ-", "FDU-AI-", "FDU-TEST-")  # 其他流程的请求邮件
OUTBOUND_MSGID_TAG = "FDU-DREP"                            # 管家回复的 Message-ID 标记
OUTBOUND_SUBJECT_PREFIX = "[FDU-DAEMON]"                   # 管家回复的主题前缀
SYSTEM_SUBJECT_PREFIX = ("晚点名签到", "晚点名签到异常")     # 系统通知邮件主题，永不当作提问
STATE = HERE / "mail_daemon_state.json"
API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-5.3-flash"

DAYS_BACK = 3            # 只处理最近 N 天的邮件（QQ 忽略 SINCE，靠客户端过滤）
MAX_REPLIES_PER_RUN = 3  # 单次运行回复上限，积压分分钟消化
REPLY_MIN_INTERVAL = 90  # 同一次运行内两封回复的最小间隔（秒）
DAILY_REPLY_CAP = 30     # 每日回复上限（跨运行持久化，防刷屏兜底）


def log(msg):
    print(f"[daemon {time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_state():
    try:
        j = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        j = {}
    if isinstance(j, list):  # 旧格式：纯 msgid 列表
        return {"processed": j, "day": "", "replies": 0}
    return {"processed": j.get("processed", []),
            "day": j.get("day", ""), "replies": int(j.get("replies", 0))}


def save_state(st, processed):
    # processed 是运行期 set（不断有新标记），落盘时以它为准；只保留最近 500 条防无限增长
    STATE.write_text(json.dumps(
        {"processed": list(processed)[-500:], "day": st["day"], "replies": st["replies"]},
        ensure_ascii=False), encoding="utf-8")


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
    acc = HERE / "accounts"
    if acc.exists():
        for d in sorted(acc.iterdir()):
            try:
                k = json.loads((d / "config.json").read_text(encoding="utf-8")).get("glm_api_key")
                if k:
                    return k
            except Exception:
                continue
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


def _hand_typed(body):
    """提取用户手打内容：QQ 引用分隔线之后全是被引用的原始邮件，整段剔除。"""
    head = body.split("---Original---")[0].split("-----原始邮件")[0]
    skip_kw = ("请直接回复", "写所需内容", "第一行写", "FDU-RQ", "FDU-AI", "发自我的")
    out = []
    for line in head.splitlines():
        s = line.strip()
        if not s or s.startswith(">") or s.lower().startswith("于 "):
            continue
        if "写道：" in s or any(k in s for k in skip_kw):
            continue
        out.append(s)
    return "\n".join(out)[:800]


def _send_reply(cfg, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = cfg["user"]
    msg["Message-ID"] = f"<{OUTBOUND_MSGID_TAG}-{int(time.time() * 1000)}@fdu-mail>"
    import smtplib
    smtp = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=25)
    try:
        smtp.login(cfg["user"], cfg["code"])
        smtp.sendmail(cfg["user"], cfg["to"], msg.as_string())
    finally:
        smtp.quit()


def main():
    import datetime
    import fcntl
    lock = open("/tmp/fdu_maildaemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        sys.exit(0)  # 上一次轮询还没结束，跳过本轮
    cfg = _key()
    st = load_state()
    processed = set(st["processed"])
    today = time.strftime("%Y%m%d")
    if st["day"] != today:  # 跨天清零每日回复计数
        st["day"] = today
        st["replies"] = 0

    imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
    imap.login(cfg["user"], cfg["code"])
    imap.select("INBOX")
    typ, data = imap.search(None, f'(FROM "{cfg["user"]}")')
    ids = (data[0] or b"").split()
    cutoff = time.time() - DAYS_BACK * 86400

    # 第一段：只抓头部做分拣（QQ 不认 SINCE，全量靠客户端过滤）
    candidates = []
    dirty = False
    for eid in ids:
        typ, md = imap.fetch(eid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE)])")
        head = email.message_from_bytes(md[0][1])
        msgid = (head.get("Message-ID", "") or "").strip()
        subject = _decode(head.get("Subject", ""))
        if msgid in processed:
            continue
        # 管家自己的出站邮件 → 记为已处理，永不回复
        if OUTBOUND_MSGID_TAG in msgid or subject.startswith(OUTBOUND_SUBJECT_PREFIX):
            processed.add(msgid)
            dirty = True
            continue
        if any(m in subject for m in SKIP_SUBJECT_SUBSTR):
            processed.add(msgid)
            dirty = True
            continue
        if subject.startswith(SYSTEM_SUBJECT_PREFIX) or "签到异常" in subject:
            processed.add(msgid)
            dirty = True
            continue
        try:
            ts = parsedate_to_datetime(head.get("Date")).timestamp()
        except Exception:
            ts = time.time()
        if ts < cutoff:  # 老邮件（含历史遗留）直接归档，不再进入候选
            processed.add(msgid)
            dirty = True
            continue
        candidates.append((eid, msgid, subject))

    # 第二段：候选邮件才抓全文（旧的在前，逐封回复）
    last_reply_ts = 0.0
    replies_this_run = 0
    for eid, msgid, subject in candidates:
        if st["replies"] >= DAILY_REPLY_CAP:
            log("已达每日回复上限，剩余候选记为已处理")
            processed.add(msgid)
            dirty = True
            continue
        if replies_this_run >= MAX_REPLIES_PER_RUN:
            log(f"本次运行已回复 {replies_this_run} 封，剩余候选下轮处理")
            break
        typ, md = imap.fetch(eid, "(BODY.PEEK[])")  # PEEK：不自动标读
        msg = email.message_from_bytes(md[0][1])
        user_msg = _hand_typed(_get_text(msg))
        if not user_msg:
            processed.add(msgid)
            dirty = True
            continue
        log(f"处理用户邮件: {subject[:60]}")
        try:
            answer = _answer(user_msg)
        except Exception as e:
            answer = f"管家处理出错: {e}"
        reply_body = (f"你的提问：\n{user_msg}\n\n管家回复：\n{answer}\n\n"
                      f"（系统时间 {time.strftime('%m-%d %H:%M')}）")
        if last_reply_ts and time.time() - last_reply_ts < REPLY_MIN_INTERVAL:
            log("同轮间隔不足，本轮跳过（不标记，下轮再试）")
            continue
        try:
            _send_reply(cfg, f"{OUTBOUND_SUBJECT_PREFIX} Re: {subject[:70]}", reply_body)
            log("已回复")
            st["replies"] += 1
            replies_this_run += 1
            last_reply_ts = time.time()
        except Exception as e:
            log(f"回复发送失败: {e}")
        processed.add(msgid)
        dirty = True
        save_state(st, processed)  # 每封即落盘，崩溃不再重刷

    imap.logout()
    if dirty:
        save_state(st, processed)
    if replies_this_run:
        log(f"本次共回复 {replies_this_run} 封")


if __name__ == "__main__":
    main()
