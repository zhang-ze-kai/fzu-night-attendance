#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""福大晚点名多人自动签到（主入口）。

目录结构（与本文件同级的 accounts/）：
  accounts/<学号>/config.json      每账号：坐标/策略/token/sso凭据/glm key/通知配置
  accounts/<学号>/sso_session.json 每账号：CAS 会话 cookie（免密续期用）
  main.py                          本文件：遍历 accounts/ 逐个签到

每账号流程：现有 token → 失效用 cookie 免密续期 → 再失效密码登录
（新环境首次登录可能触发短信 2FA → **邮箱双向通道**：系统发请求邮件，用户直接回复
验证码，脚本读回复后继续自治，无需打开电脑）。
异常时值守 AI 多轮决策（换会话/等待/向用户提问/上报），持续失败升级代码医生
（AI 代码级诊断 + 函数级补丁热生效）。
"""

import argparse
import importlib
import json
import random
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from checkin import (AttnClient, STATE_ABORT, STATE_DONE, STATE_FAIL, STATE_WAIT,
                     log, run_once, save_token, sso_refresh)
from checkin import sso_cookie_refresh


def account_dirs(pattern=None):
    acc = BASE_DIR / "accounts"
    if not acc.exists():
        return []
    dirs = sorted([d for d in acc.iterdir() if d.is_dir() and (d / "config.json").exists()])
    if pattern:
        dirs = [d for d in dirs if pattern in d.name]
    return dirs


def mailbox_roundtrip(acc_dir, base_dir, question, deadline, max_wait_s=1500):
    """邮箱双向通道：发请求邮件 → 轮询用户回复。返回回复文本或 None。"""
    from mailbox import load_notify, request_and_wait
    ncfg = load_notify(str(base_dir))
    if not ncfg:
        log("[邮箱通道] notify.json 未配置，无法向用户提问")
        return None
    remaining = deadline - time.time() - 600
    if remaining < 120:
        log(f"[邮箱通道] 窗口/时限剩余不足（{int(remaining)}s），放弃提问")
        return None
    marker = f"FDU-RQ-{acc_dir}-{int(time.time())}"
    timeout_s = int(min(max_wait_s, remaining))
    log(f"[邮箱通道] 请求邮件已发送，等待用户回复（最长 {timeout_s}s）…")
    r = request_and_wait(ncfg, marker, question, timeout_s=timeout_s, poll_s=25,
                         deadline_ts=deadline - 300)
    reply = (r.get("answer") or "").strip()
    log("[邮箱通道] " + ("收到回复: " + reply[:80] if reply else "超时未收到回复"))
    return reply or None


def run_account(acc_dir, dry_run=False, once=False):
    """跑单个账号的完整签到循环。返回 True=已签到/成功，False=失败。"""
    cfg_path = acc_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    def loga(msg):
        log(f"[{acc_dir.name}] {msg}")

    token = cfg.get("token")
    if not token:
        nt = sso_cookie_refresh(cfg, str(cfg_path))
        token = nt or sso_refresh(cfg, str(cfg_path))
    client = AttnClient(token, cfg.get("base_url"), user_agent=cfg.get("user_agent"))

    deadline = time.time() + int(cfg.get("max_wait_minutes", 120)) * 60
    relogins, err_streak = 0, 0
    ai_rounds = 0            # AI 白名单决策轮数（最多 3，之后升级代码医生）
    asked_human = False      # AI 向用户提问（邮件往返）最多一次
    doctor_done = False      # 代码医生每轮运行最多介入一次
    notified_fail = False    # 失败邮件只发一次
    notified_manual = False  # 22:00 手动处理提醒只发一次
    last_err = ""

    while True:
        try:
            state = run_once(client, cfg, dry_run=dry_run)
            err_streak = 0
        except Exception as e:
            msg = str(e)
            if "token 可能已失效" in msg and relogins < 3:
                relogins += 1
                loga(f"token 失效，第 {relogins}/3 次续期…")
                try:
                    nt = sso_cookie_refresh(cfg, str(cfg_path)) or sso_refresh(cfg, str(cfg_path))
                    client = AttnClient(nt, cfg.get("base_url"), user_agent=cfg.get("user_agent"))
                    continue
                except Exception as e2:
                    loga(f"续期失败: {e2}")
                    # 邮箱双向通道：短信墙 → 请求邮件 → 用户回复验证码 → 继续自治
                    if "短信二次认证" in str(e2):
                        code_reply = mailbox_roundtrip(
                            acc_dir.name, BASE_DIR,
                            f"账号 {acc_dir.name} 需要短信验证码：请回复本邮件，第一行写你手机收到的验证码。",
                            deadline, max_wait_s=1800)
                        if code_reply:
                            m = re.search(r"(\d{4,8})", code_reply.replace(" ", ""))
                            if m:
                                (acc_dir / "sms_code.txt").write_text(m.group(1), encoding="utf-8")
                                loga(f"[邮箱通道] 收到验证码 {m.group(1)}，运行引导…")
                                subprocess.run([sys.executable, str(BASE_DIR / "bootstrap.py"),
                                                acc_dir.name, "1800"], cwd=str(BASE_DIR),
                                               timeout=2600)
                                loga("[邮箱通道] 引导完成，重新续期…")
                                nt = sso_cookie_refresh(cfg, str(cfg_path)) or sso_refresh(cfg, str(cfg_path))
                                client = AttnClient(nt, cfg.get("base_url"), user_agent=cfg.get("user_agent"))
                                notified_fail = False
                                continue
                        loga("[邮箱通道] 未完成验证（超时/格式不对），转人工")
                        notify_account_issue(acc_dir, cfg,
                                             f"短信验证邮件往返未成功: {str(e2)[:200]}",
                                             "请手动打开 App 签到（22:30 前不算迟到），或重跑 bootstrap.py")
                        return False
                    notify_account_issue(acc_dir, cfg, f"token 续期失败: {e2}", "需人工处理")
                    return False
            last_err = f"{type(e).__name__}: {msg}"
            loga(f"异常: {msg}")
            err_streak += 1
            state = STATE_WAIT
            if once:
                return False

            # AI 白名单决策（最多 3 轮）
            if err_streak >= 2 and ai_rounds < 3:
                ai_rounds += 1
                try:
                    from ai_assist import ai_decide
                    act, reason = ai_decide(
                        f"账号{acc_dir.name}签到连续异常",
                        {"error_history": last_err[:400], "streak": err_streak,
                         "relogins_done": relogins, "ai_round": ai_rounds},
                        api_key=cfg.get("glm_api_key"))
                    loga(f"[AI 决策#{ai_rounds}] {act} — {reason}")
                    if act == "renew_session" and relogins < 3:
                        relogins += 1
                        nt = sso_cookie_refresh(cfg, str(cfg_path)) or sso_refresh(cfg, str(cfg_path))
                        if nt:
                            client = AttnClient(nt, cfg.get("base_url"), user_agent=cfg.get("user_agent"))
                            loga("[AI 处置] 已换新 token，下一轮重试")
                            continue
                        loga("[AI 处置] 续期未成功")
                    elif act == "ask_human" and not asked_human:
                        asked_human = True
                        reply = mailbox_roundtrip(acc_dir.name, BASE_DIR,
                                                  reason + "\n\n请回复本邮件，第一行写你的答复。",
                                                  deadline, max_wait_s=1200)
                        if reply:
                            ai_advice(acc_dir, cfg,
                                      f"用户对问题的回答：{reply[:200]}。原问题：{reason[:150]}。给出最终处置建议。")
                    elif act == "escalate":
                        notify_account_issue(acc_dir, cfg, last_err, reason)
                        ai_rounds = 99  # 已上报人工，停止自动决策（保持重试等恢复）
                except Exception as ae:
                    loga(f"[AI 处置异常] {ae}")

            time.sleep(60 if err_streak > 1 else 30)

        # 代码医生：运维与 AI 白名单决策都无法解决时，AI 接入代码级诊断/修补
        doctor_trigger = ((state == STATE_FAIL and ai_rounds >= 3) or
                          (state == STATE_WAIT and err_streak >= 3 and ai_rounds >= 3))
        if doctor_trigger and not doctor_done:
            doctor_done = True
            loga("[代码医生] 运维修复未奏效，AI 接入代码级诊断…")
            try:
                from code_doctor import diagnose_and_maybe_patch
                lr = getattr(client, "last_response", None) or {}
                res = diagnose_and_maybe_patch(
                    f"账号{acc_dir.name}签到持续失败",
                    {"last_response": lr, "error_history": last_err[:300],
                     "account": acc_dir.name, "server_time": time.strftime("%Y-%m-%d %H:%M")},
                    failing_action=(lr or {}).get("action", "check.action"),
                    api_key=cfg.get("glm_api_key"),
                    app_dir=str(BASE_DIR),
                    dry_run=dry_run)
                loga(f"[代码医生] patched={res.get('patched')} "
                     f"rolled_back={res.get('rolled_back')} | {res.get('notify_text')[:260]}")
                notify_account_issue(acc_dir, cfg,
                                     "自动修复升级至代码级： " + res.get("notify_text", "")[:200],
                                     res.get("diagnosis", "")[:300])
                if res.get("patched") and not dry_run:
                    # 补丁热生效：重载模块 + 全局符号更新 + 重建客户端
                    import checkin as _ck
                    importlib.reload(_ck)
                    globals().update({
                        "run_once": _ck.run_once, "AttnClient": _ck.AttnClient,
                        "STATE_DONE": _ck.STATE_DONE, "STATE_WAIT": _ck.STATE_WAIT,
                        "STATE_FAIL": _ck.STATE_FAIL, "STATE_ABORT": _ck.STATE_ABORT,
                        "sso_cookie_refresh": _ck.sso_cookie_refresh,
                        "sso_refresh": _ck.sso_refresh})
                    client = _ck.AttnClient(cfg.get("token"), cfg.get("base_url"),
                                            user_agent=cfg.get("user_agent"))
                    err_streak = 0
                    loga("[代码医生] 补丁已热生效，继续窗口内重试")
                    continue
            except Exception as de:
                loga(f"[代码医生异常] {de}")
                notify_account_issue(acc_dir, cfg, "代码医生自身出错: " + str(de)[:200], "")

        if state == STATE_DONE:
            return True
        if state == STATE_FAIL:
            # 用户策略：失败立即邮件 + AI 自主修复；22:00 仍未成 → 提醒手动
            last_fail_reason = getattr(client, "last_fail_reason", "签到校验/提交失败")
            if not notified_fail:
                notified_fail = True
                notify_account_issue(acc_dir, cfg, "签到失败：" + last_fail_reason,
                                     "AI 正在自动修复；成功后另行通知。若 22:00 前未成功会再提醒你手动处理")
            if ai_rounds < 3:
                ai_rounds += 1
                try:
                    from ai_assist import ai_decide
                    act, reason = ai_decide(f"账号{acc_dir.name}签到失败", {
                        "reason": last_fail_reason, "relogins_done": relogins,
                        "ai_round": ai_rounds}, api_key=cfg.get("glm_api_key"))
                    loga(f"[AI 决策#{ai_rounds}] {act} — {reason}")
                    if act == "renew_session" and relogins < 3:
                        relogins += 1
                        nt = sso_cookie_refresh(cfg, str(cfg_path)) or sso_refresh(cfg, str(cfg_path))
                        if nt:
                            client = AttnClient(nt, cfg.get("base_url"), user_agent=cfg.get("user_agent"))
                            loga("[AI 处置] 已换新 token，下一轮重试")
                    elif act == "ask_human" and not asked_human:
                        asked_human = True
                        reply = mailbox_roundtrip(acc_dir.name, BASE_DIR,
                                                  reason + "\n\n请回复本邮件，第一行写你的答复。",
                                                  deadline, max_wait_s=1200)
                        if reply:
                            ai_advice(acc_dir, cfg,
                                      f"用户回答：{reply[:200]}。原问题：{reason[:150]}。给出最终处置建议。")
                    elif act == "escalate":
                        notify_account_issue(acc_dir, cfg, last_fail_reason, reason)
                        ai_rounds = 99
                except Exception as ae:
                    loga(f"[AI 处置异常] {ae}")
        if state == STATE_ABORT:
            loga("配置/数据问题中止（详见上方日志）")
            return False
        # 用户策略：22:00 仍未签上（且今晚确实有清单）→ 提醒手动处理
        if (notified_fail and not notified_manual and getattr(client, "saw_plan", False)
                and client.server_now_minutes() >= 22 * 60):
            notified_manual = True
            notify_account_issue(acc_dir, cfg,
                                 "自动修复至今未成功，请手动打开智汇福大 App 完成签到",
                                 "注意 22:30 前提交不算迟到；脚本仍会在窗口内继续尝试")
        if once:
            return False
        if time.time() > deadline:
            loga("等待超时退出")
            return False
        # WAIT：随机抖动 60-90s，避免多账号同秒轮询
        time.sleep(random.uniform(60, 90))


def notify_account_issue(acc_dir, cfg, error, ai_reason=""):
    from notifier import push, load_notify_config
    ch, key = load_notify_config(str(BASE_DIR), cfg)
    content = "错误: " + str(error)[:300] + "\nAI 判断: " + str(ai_reason)[:200] + "\n请查看 run.log 或联系维护者处理。"
    r = push(f"晚点名签到异常-{acc_dir.name}", content, ch, key)
    log(f"[{acc_dir.name}] [通知] {r}")


def ai_advice(acc_dir, cfg, issue):
    try:
        from ai_assist import ai_help
        advice = ai_help(issue, api_key=cfg.get("glm_api_key"))
        log(f"[{acc_dir.name}] [AI 建议] {advice}")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="福大晚点名多人签到")
    ap.add_argument("--account", help="只跑指定账号（子串匹配目录名）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="每账号只判定一轮")
    ap.add_argument("--list", action="store_true", help="列出账号")
    args = ap.parse_args()

    dirs = account_dirs(args.account)
    if args.list or not dirs:
        for d in account_dirs():
            print("账号:", d.name)
        if not dirs and not args.list:
            sys.exit("没有匹配的账号目录（accounts/<学号>/config.json）")
        return

    ok, fail = [], []
    # 日志轮转：run.log 超 5MB 归档为 run-YYYYMMDD.log
    rl = BASE_DIR / "run.log"
    if rl.exists() and rl.stat().st_size > 5 * 1024 * 1024:
        rl.rename(BASE_DIR / ("run-" + time.strftime("%Y%m%d") + ".log"))
        log("旧日志已归档")
    for d in dirs:
        log(f"===== 账号 {d.name} 开始 =====")
        try:
            result = run_account(d, args.dry_run, args.once)
            (ok if result else fail).append(d.name)
        except Exception:
            traceback.print_exc()
            fail.append(d.name)
        # 账号间随机间隔 3-8 分钟（防同 IP 同步行为特征）；--once 快速模式跳过
        if d != dirs[-1] and not args.once:
            gap = random.uniform(180, 480)
            log(f"账号间隔 {gap:.0f}s 后处理下一个\n")
            time.sleep(gap)

    log(f"===== 汇总: 成功 {len(ok)} {ok} / 失败 {len(fail)} {fail} =====")
    if args.dry_run:
        log("dry-run 模式不推送通知")
        sys.exit(0 if not fail else 1)
    try:
        from notifier import push, load_notify_config
        ch, key = load_notify_config(str(BASE_DIR))
        title = "晚点名签到" + ("全部成功" if not fail else f"完成: {len(ok)}成功/{len(fail)}失败")
        content = "成功: " + json.dumps(ok, ensure_ascii=False) + "\n失败: " + json.dumps(fail, ensure_ascii=False) + "\n时间: " + time.strftime("%Y-%m-%d %H:%M")
        if fail:
            content += "\n失败账号请查看 run.log 与 [AI 建议]"
        r = push(title, content, ch, key)
        log(f"[通知] {r}")
    except Exception as e:
        log(f"[通知异常] {e}")
    sys.exit(0 if not fail else 1)


if __name__ == "__main__":
    main()
