#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSO 交互式引导：无头浏览器完成 密码登录 → 短信2FA → 保存会话。

用法：python3 bootstrap.py <学号>   （账号目录 accounts/<学号>/，与本脚本同级）
状态文件（账号目录内）：
  bootstrap_status.json  每步进度/错误，外部轮询
  sms_code.txt           外部写入短信验证码（纯数字），本脚本轮询等待
产物：
  sso_session.json       登录后的全部 cookie（长期复用）
  config.json 的 token   顺带更新为最新 H5 token

依赖（仅首次）：pip install playwright && playwright install chromium
"""
import json
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("缺少 playwright：请先执行  pip install playwright && playwright install chromium")

# 可移植路径：accounts/<学号>/ 相对于本脚本所在目录
APP_DIR = Path(__file__).resolve().parent
if len(sys.argv) < 2:
    sys.exit("用法: python bootstrap.py <学号>  （账号目录 accounts/<学号>/ 需已建好 config.json）")
ACC = sys.argv[1]
BASE = APP_DIR / "accounts" / ACC
if not (BASE / "config.json").exists():
    sys.exit(f"账号目录不存在: {BASE}（请先建 config.json）")
SERVICE = "https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/oauth2/callback.action"
LOGIN_URL = "https://sso.fzu.edu.cn/login?service=" + SERVICE

cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))


def status(**kw):
    st = {"ts": time.strftime("%H:%M:%S")}
    st.update(kw)
    (BASE / "bootstrap_status.json").write_text(
        json.dumps(st, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(st, ensure_ascii=False), flush=True)


def shot(page, name):
    try:
        page.screenshot(path=str(BASE / f"bs_{name}.png"), full_page=True)
        (BASE / f"bs_{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        status(step=f"截图失败 {name}: {e}")


def main():
    status(step="启动浏览器")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40000)

        # 等 Angular 渲染出账密表单
        page.wait_for_selector("input[name=username], input[ngModel]", timeout=30000)
        time.sleep(2)
        shot(page, "01_login")

        # 填账号
        user_sel = "input[name=username]"
        page.fill(user_sel, cfg["sso_username"])
        # 密码框：页面上 type=password 的可见输入
        pwd = page.query_selector("input[type=password]") or page.query_selector("input[nz-input][ngModel]")
        if not pwd:
            status(step="错误", error="找不到密码框")
            shot(page, "err_nopwd")
            sys.exit(1)
        pwd.fill(cfg["sso_password"])
        status(step="账密已填，点击登录")

        # 点登录按钮
        clicked = False
        for sel in ["button:has-text('登 录')", "button:has-text('登录')",
                    "button[type=submit]", ".login-btn", "#login-btn"]:
            try:
                page.click(sel, timeout=3000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            status(step="错误", error="找不到登录按钮")
            sys.exit(1)

        # 等待响应：成功→切到 smsLogin 页（SPA 无跳转）；失败→错误提示
        time.sleep(5)
        shot(page, "02_after_submit")

        # 找「获取验证码」按钮（smsLogin 页标志）
        code_btn = None
        for sel in ["button:has-text('获取验证码')", "span:has-text('获取验证码')",
                    "*:has-text('获取验证码')"]:
            try:
                page.wait_for_selector(sel, timeout=15000)
                code_btn = page.query_selector(sel)
                break
            except Exception:
                continue
        if not code_btn:
            status(step="错误", error="未进入短信验证页（密码可能错误或页面变化）")
            sys.exit(2)
        status(step="短信验证页已出现，点击获取验证码")
        code_btn.click()
        status(phase="sms_sent", msg="短信已发送，等待验证码写入 sms_code.txt")
        shot(page, "03_sms_sent")

        # 轮询等待验证码（最多 5 分钟）
        code = None
        deadline = time.time() + 300
        while time.time() < deadline:
            f = BASE / "sms_code.txt"
            if f.exists():
                code = f.read_text(encoding="utf-8").strip()
                if code:
                    break
            time.sleep(2)
        if not code:
            status(step="错误", error="5 分钟内未收到验证码")
            sys.exit(3)
        status(step=f"收到验证码 {code}，填入提交")

        # 填验证码：找验证码输入框
        filled = False
        for sel in ["input[placeholder*='验证码']", "input[name=captcha_code]",
                    "input[inputmode=numeric]", "input[maxlength='6']",
                    "input[type=tel]"]:
            try:
                page.fill(sel, code, timeout=3000)
                filled = True
                break
            except Exception:
                continue
        if not filled:
            status(step="错误", error="找不到验证码输入框")
            shot(page, "err_nocodeinput")
            sys.exit(4)

        # 提交：页面上此时的确认按钮
        submitted = False
        for sel in ["button:has-text('登 录')", "button:has-text('登录')",
                    "button:has-text('确 定')", "button:has-text('确定')",
                    "button[type=submit]"]:
            try:
                page.click(sel, timeout=3000)
                submitted = True
                break
            except Exception:
                continue
        status(step="验证码已提交，等待跳转")
        shot(page, "04_code_submitted")

        # 等待跳转链路到 index.action?token=
        token = None
        deadline = time.time() + 40
        while time.time() < deadline:
            url = page.url
            if "token=" in url:
                token = url.split("token=")[1].split("&")[0]
                break
            time.sleep(1)
        shot(page, "05_final")
        cookies = ctx.cookies()
        (BASE / "sso_session.json").write_text(
            json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
        if token:
            cfg["token"] = token
            (BASE / "config.json").write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            status(phase="done", msg="登录成功", token_prefix=token[:14],
                   cookies=len(cookies))
        else:
            status(phase="partial", msg="会话已保存但未捕获 token（明天可用 cookie 换）",
                   cookies=len(cookies))
        browser.close()


if __name__ == "__main__":
    main()
