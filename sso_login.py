#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""福大 SSO(CAS) 自动登录 → 晚点名 H5 token 换取。

链路（与智汇福大 App WebView 实际加载顺序一致）：
  1. GET  sso.fzu.edu.cn/login?service=<callback>   拿登录页: execution(flowkey)+croypto(16B AES key)
  2. POST sso.fzu.edu.cn/login                      表单: username/password(AES-ECB)/execution/croypto...
     → 302 带 ?ticket=ST-... 回 callback
  3. callback.action?ticket=...                      → 服务端 Set-Cookie / 302 到 authorize
  4. authorize.action?token=...                      → 最终 token（即 H5 请求头 token）

密码加密（cas-login main bundle 实测）：AES-128-ECB / PKCS7 / key=Base64decode(login-croypto)，输出 Base64。
"""

import base64
import re
import sys

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

SSO_BASE = "https://sso.fzu.edu.cn"
DEFAULT_CALLBACK = "https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/oauth2/callback.action"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def log(msg):
    print(f"[sso] {msg}", file=sys.stderr, flush=True)


def aes_ecb_encrypt(key_b64, plaintext):
    key = base64.b64decode(key_b64)
    return base64.b64encode(
        AES.new(key, AES.MODE_ECB).encrypt(pad(plaintext.encode(), 16))
    ).decode()


def sso_login(username, password, service=DEFAULT_CALLBACK, session=None):
    """走完 CAS 登录链路，返回 (session, token, final_url)。

    session 已带 SSO + yzsxg 的 Cookie，可直接复用。
    失败抛 RuntimeError。
    """
    s = session or requests.Session()
    s.headers.update({"User-Agent": DESKTOP_UA})
    s.verify = True

    # 1. 登录页
    r = s.get(f"{SSO_BASE}/login", params={"service": service}, timeout=20)
    r.raise_for_status()
    html = r.text

    def grab(pid):
        m = re.search(rf'id="{pid}"[^>]*>([^<]*)<', html)
        return m.group(1).strip() if m else None

    execution = grab("login-page-flowkey")
    croypto = grab("login-croypto")
    if not execution or not croypto:
        raise RuntimeError(f"登录页缺少 flowkey/croypto（可能触发验证码或页面改版）: "
                           f"execution={execution!r} croypto={croypto!r}")
    captcha_url = grab("captcha-url") or ""
    log(f"execution={execution[:24]}… croypto={croypto} captcha_url={captcha_url!r}")
    if captcha_url:
        raise RuntimeError(f"需要验证码（captcha-url={captcha_url}），暂不支持")

    # 2. 预检：查询该账号的验证码状态（页面 onSubmit 提交前的 XHR，可能影响服务端 flow 状态）
    s.get(f"{SSO_BASE}/api/protected/user/findCaptchaCount/{username}", timeout=20)

    # 3. 表单 POST（与浏览器一致：action="login" 相对路径 → POST /login 不带 query；
    #    字段集与 cas-login bundle 的 onSubmit 完全一致）
    form = {
        "type": "UsernamePassword",
        "_eventId": "submit",
        "geolocation": "",
        "execution": execution,
        "croypto": croypto,
        "username": username,
        "password": aes_ecb_encrypt(croypto, password),
        # 无验证码时固定为加密的 "{}"（页面 captcha_payload={} 的 JSON 序列化）
        "captcha_payload": aes_ecb_encrypt(croypto, "{}"),
    }
    r = s.post(f"{SSO_BASE}/login", data=form, allow_redirects=False, timeout=20,
               headers={"Origin": SSO_BASE, "Referer": r.url})
    log(f"POST /login -> HTTP {r.status_code}")

    if r.status_code == 200:
        body = r.text
        m = re.search(r'id="current-login-type"[^>]*>([^<]*)<', body)
        login_type = m.group(1).strip() if m else ""
        if login_type == "smsLogin" or "login-croypto" not in body:
            raise RuntimeError(
                "密码已验证通过，但服务端要求短信二次认证（新环境首次登录）——"
                "请运行 bootstrap.py 完成一次短信验证，会话 cookie 保存后即可长期复用"
            )
        err = re.search(r'id="login-error-code"[^>]*>([^<]*)<', body)
        page_err = re.search(r'class="error[^"]*"[^>]*>([^<]{2,80})<', body)
        raise RuntimeError(
            f"登录失败(HTTP 200 无跳转): error_code={err and err.group(1)!r} "
            f"page_err={page_err and page_err.group(1)!r} — 多为账号密码错误"
        )

    # 4. 跟随重定向链直到拿到 token（服务端会返回相对路径 Location）
    from urllib.parse import urljoin
    hops, token = 0, None
    url, cur = r.headers.get("Location"), "https://sso.fzu.edu.cn/login"
    while url and hops < 10:
        hops += 1
        full = url if url.startswith("http") else urljoin(cur, url)
        log(f"  -> {full[:110]}")
        m = re.search(r"[?&]token=([A-Za-z0-9_\-]+)", full)
        if m:
            return s, m.group(1), full
        r = s.get(full, allow_redirects=False, timeout=20)
        cur = full
        url = r.headers.get("Location")

    raise RuntimeError(f"链路走完没拿到 token（{hops} 跳）— 最后响应: HTTP {r.status_code}")


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="SSO 登录换晚点名 token")
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--config", default="config.json", help="从 config 读 sso_username/sso_password")
    args = ap.parse_args()

    if not args.username or not args.password:
        cfg = json.load(open(args.config, encoding="utf-8"))
        args.username = args.username or cfg.get("sso_username")
        args.password = args.password or cfg.get("sso_password")
    if not args.username or not args.password:
        sys.exit("需要 --username --password 或 config.json 里的 sso_username/sso_password")

    _, token, url = sso_login(args.username, args.password)
    print(f"\nTOKEN: {token}")
    print(f"URL:   {url}")
