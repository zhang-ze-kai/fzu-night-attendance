#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""福大晚点名自动签到脚本。

对应 yzsxg.fzu.edu.cn 的 livecloud/project/fzu/attn H5 应用：
  init.action             POST JSON {}    拉取考勤计划/时间窗/围栏/个人状态
  currentTimestamp.action GET             服务端校时
  check.action            GET param=AES() 服务端判定定位是否在围栏内
  clockIn.action          GET param=AES() 提交打卡

所有请求带请求头 token: <index.action URL 里的 token>。
AES-128-CBC / PKCS7 / key=IV="apexinfoapexinfo"，Base64 后 urlencode 进 GET param。
"""

import argparse
import base64
import json
import random
import re
import sys
from pathlib import Path
import time
from datetime import datetime

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

BASE_URL = "https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/"
AES_KEY = b"apexinfoapexinfo"  # 与页面 JS 硬编码一致，key = IV

# 与模拟器 WebView 真实指纹一致（Android 15 / WebView 124 / sdk_gphone64_x86_64）
DEFAULT_UA = (
    "Mozilla/5.0 (Linux; Android 15; sdk_gphone64_x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/124.0.6367.219 Mobile Safari/537.36 appScheme=kysk-fdxy-app"
)

STATE_DONE = "done"          # 已签到 / 无需签到
STATE_WAIT = "wait"          # 条件未满足，继续轮询
STATE_ABORT = "abort"        # 配置/服务端数据问题，无法继续
STATE_FAIL = "fail"          # 真实失败（围栏未命中/提交失败）：通知一次后继续修复重试

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def hm_to_minutes(hm):
    """'21:00'（可能带尾部空格）-> 当天第几分钟；None 返回 None。"""
    if not hm:
        return None
    h, m = hm.strip().split(":")
    return int(h) * 60 + int(m)


class AttnClient:
    def __init__(self, token, base_url=BASE_URL, timeout=15, user_agent=None):
        self.token = token
        self.base = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "token": token,
            # Android WebView 会给所有请求带上 X-Requested-With: <应用包名>
            "User-Agent": user_agent or DEFAULT_UA,
            "X-Requested-With": "cn.edu.fzu.fdxypa",
            "Referer": base_url.replace("/livecloud/", "/plug-in/livecloud/") + "index.action?token=" + token,
        })
        self.server_offset_ms = 0  # 服务端时间 - 本机时间

    # ---- 加密（与页面 CryptoJS 等价） ----

    def encrypt(self, obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        c = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY)
        return base64.b64encode(c.encrypt(pad(raw, 16))).decode()

    def decrypt(self, b64):
        c = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY)
        return json.loads(unpad(c.decrypt(base64.b64decode(b64)), 16))

    # ---- 服务端时间 ----

    def server_now_ms(self):
        try:
            r = self.s.get(self.base + "currentTimestamp.action", timeout=self.timeout)
            ts = self._unwrap(r)
            if isinstance(ts, dict):
                ts = ts.get("timestamp")
            self.server_offset_ms = int(ts) - int(time.time() * 1000)
        except Exception as e:
            log(f"服务端时间获取失败（降级用本地时间）: {e}")
        return int(time.time() * 1000) + self.server_offset_ms

    def server_now_minutes(self):
        now = datetime.fromtimestamp(self.server_now_ms() / 1000)
        return now.hour * 60 + now.minute

    # ---- 业务接口 ----

    @staticmethod
    def _unwrap(r):
        try:
            j = r.json()
        except ValueError:
            # 假/过期 token 会返回 HTTP 500 + HTML 提示页
            raise RuntimeError(f"token 可能已失效（HTTP {r.status_code} 非 JSON 响应），请重新抓取 token")
        if not (j.get("success") and j.get("code") == 1):
            raise RuntimeError(f"接口返回失败: HTTP {r.status_code} {json.dumps(j, ensure_ascii=False)[:300]}")
        return j.get("data")

    def init(self):
        r = self.s.post(self.base + "init.action", json={}, timeout=self.timeout)
        return self._unwrap(r) or {}

    def check(self, lon, lat, school_position):
        param = self.encrypt({
            "mapType": "amap",
            "longitude": lon,
            "latitude": lat,
            "range": json.dumps(school_position, separators=(",", ":")),
        })
        return self._encrypted_get("check.action", param)

    def _encrypted_get(self, action, b64_param):
        """GET param=加密串。浏览器实际是双重 URL 编码（encodeURIComponent + axios 序列化），
        服务端解两次；这里先 quote 一次、requests 的 params 序列化补第二层，与之对齐。
        单层编码会让服务端二次解码时把 base64 的 + 解成空格 → 解密损坏 → 校验异常。"""
        from urllib.parse import quote
        param = quote(b64_param, safe="")
        r = self.s.get(self.base + action, params={"param": param}, timeout=self.timeout)
        return self._unwrap(r)

    def clock_in(self, payload):
        return self._encrypted_get("clockIn.action", self.encrypt(payload))


def jitter_coord(v, meters=30):
    """给坐标加 ±meters 米左右的随机抖动（真实 GPS 每次定位都不会完全相同）。

    纬度 1° ≈ 111km，0.0003° ≈ 33m；抖动后仍在校园围栏内（围栏通常覆盖整个校区）。
    """
    return round(v + random.uniform(-meters, meters) / 111000, 6)


def pick_campus_id(campus):
    for k in ("id", "ID", "FID", "FId"):
        if campus.get(k) is not None:
            return campus[k]
    raise RuntimeError(f"校区对象里找不到 ID 字段: {json.dumps(campus, ensure_ascii=False)[:200]}")


def pick_campus_name(campus):
    for k in ("FName", "name", "Name", "title"):
        if campus.get(k):
            return campus[k]
    return ""


def run_once(client, cfg, dry_run=False):
    """单次状态机判定。返回 STATE_*。"""
    client.in_window = False
    data = client.init()
    params = data.get("paramsData") or {}
    user = data.get("userData")
    status = data.get("statusData")
    schools = data.get("schoolData") or []

    # 1. 已签到 / 处理中
    if status and status.get("status") in ("SUCCESS", "PROCESSING"):
        log(f"今日已签到: status={status.get('status')} 打卡时间={status.get('createTime')}")
        return STATE_DONE

    # 2. 无考勤计划（清单未下发）
    if not user or not user.get("FPlanID"):
        log("无晚点名清单（userData=null 或 FPlanID 为空），晚些时候再试")
        return STATE_WAIT
    client.saw_plan = True   # 计划已下发（22:00 未成功提醒的触发前提）

    way = user.get("FWay")
    if way == 3:
        log("FWay=3 无需打卡")
        return STATE_DONE

    # 3. 时间窗（用服务端时间）
    start_m = hm_to_minutes(params.get("FStartTime"))
    end_m = hm_to_minutes(params.get("FEndTime"))
    late_m = hm_to_minutes(params.get("FLateTime"))
    now_m = client.server_now_minutes()
    if start_m is None or end_m is None:
        log(f"未配置时间窗: FStartTime={params.get('FStartTime')} FEndTime={params.get('FEndTime')}")
        return STATE_ABORT
    if not (start_m <= now_m <= end_m):
        log(f"不在签到时间范围 [{params.get('FStartTime')}-{params.get('FEndTime')}]，服务端时间当前为第 {now_m} 分钟")
        return STATE_WAIT
    client.in_window = True

    # 4. 定位判定（每次运行加抖动，避免每晚上报完全相同的坐标）
    lon = jitter_coord(cfg["lon"], cfg.get("jitter_meters", 30))
    lat = jitter_coord(cfg["lat"], cfg.get("jitter_meters", 30))
    address = cfg.get("address", "")
    actual_location = address

    if way == 1:
        # 校内定位：必须有围栏 + check 命中
        if not schools:
            log("FWay=1 但 schoolData 无围栏（无签到范围）")
            return STATE_ABORT
        position = schools[0].get("FPosition")
        if position is None:
            log("围栏对象缺 FPosition")
            return STATE_ABORT
        if isinstance(position, str):
            position = json.loads(position)
        # position 形如 {"range":[{"type":"polygon","paths":[[lng,lat]...],"radius":[]}]}
        school_position = position if "range" in position else {"range": [position]}
        try:
            hits = client.check(lon, lat, school_position)
        except Exception as ce:
            log(f"check.action 异常（窗口内服务器抖动？60s 后重试）: {ce}")
            return STATE_WAIT
        log(f"check.action 命中 {len(hits)} 个校区: {[pick_campus_name(c) for c in hits]}")
        if not hits:
            # 偶发抖动出栏/围栏当晚更新：下一轮重新抖动再试；连续失败由外层重试机制兜底
            client.last_fail_reason = "定位未命中围栏（坐标错误或当晚围栏有变）"
            log("定位未命中围栏（抖动出栏或围栏变化），下一轮重试")
            return STATE_FAIL
        campus = hits[0]
        campus_id = pick_campus_id(campus)
        actual_location = f"{pick_campus_name(campus)} {address}".strip()
    else:
        # 外勤（way=2）：只要求有地址
        if not address:
            log("FWay=2 外勤模式但未配置 address")
            return STATE_ABORT
        campus_id = ""

    # 5. 迟到判定（与页面一致：服务端时间比 FLateTime 晚则为 1）
    is_late = 1 if (late_m is not None and now_m > late_m) else 0

    # 模拟页面「每 10 秒定位轮询」的节奏：提交前再做几次 check（间隔 10±2s），
    # 让服务端看到的请求序列像真实页面（多次定位校验 → 点击签到），而不是一发 check 紧跟 clockIn
    if way == 1 and not cfg.get("skip_warmup"):
        warmup = int(cfg.get("warmup_checks", 2))
        for i in range(warmup):
            time.sleep(random.uniform(8, 12))
            log(f"定位轮询模拟 {i + 1}/{warmup}")
            try:
                client.check(lon, lat, school_position)
            except Exception as we:
                log(f"  轮询模拟异常（忽略继续）: {we}")

    payload = {
        "campus": campus_id,
        "lon": lon,
        "lat": lat,
        "startTime": params.get("FStartTime"),
        "endTime": params.get("FEndTime"),
        "actualLocation": actual_location,
        "caIsNo": cfg.get("ca_is_no", "0"),
        "collUnit": user.get("FCollUnit"),
        "way": way,
        "isLate": is_late,
    }
    log(f"准备打卡: {json.dumps(payload, ensure_ascii=False)}")
    if dry_run:
        log("dry-run：不提交 clockIn.action")
        return STATE_DONE

    # 提交前随机等待：避免每晚在固定时刻（如整 21:05:00）打卡
    delay_range = cfg.get("clockin_delay_range", [30, 180])
    delay = random.uniform(delay_range[0], delay_range[1])
    log(f"等待 {delay:.0f}s 后提交（模拟手动点击时机）")
    time.sleep(delay)

    # 提交重试：晚点峰值服务器可能 500/超时，快速重试直到成功或次数用尽
    attempts = int(cfg.get("submit_retries", 8))
    last_submit_err = "unknown"
    for i in range(attempts):
        try:
            result = client.clock_in(payload)
            attn = result.get("attn") if isinstance(result, dict) else result
            log(f"打卡返回: {json.dumps(attn or result, ensure_ascii=False)}")
            return STATE_DONE
        except Exception as e:
            last_submit_err = f"{type(e).__name__}: {e}"
            log(f"提交失败 ({i + 1}/{attempts}): {e}")
            if i < attempts - 1:
                gap = random.uniform(20, 45)
                log(f"  {gap:.0f}s 后重试提交…")
                time.sleep(gap)
    client.last_fail_reason = "clockIn 提交连续失败: " + last_submit_err
    log("提交重试次数用尽，转入窗口内快速轮询（整条流程重新走）")
    try:
        from ai_assist import ai_help
        advice = ai_help("clockIn 提交连续失败", {"last_error": str(last_submit_err), "payload_keys": list(payload.keys())})
        log(f"[AI 建议] {advice}")
    except Exception:
        pass
    return STATE_FAIL


def save_token(cfg_path, cfg, token):
    cfg["token"] = token
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    log(f"新 token 已写入 {cfg_path}")


def sso_refresh(cfg, cfg_path):
    """用 SSO 学号密码登录换新 token（无凭据则报错）。"""
    if not (cfg.get("sso_username") and cfg.get("sso_password")):
        raise RuntimeError("token 已失效且未配置 sso_username/sso_password，无法自动续期")
    from sso_login import sso_login
    _, token, _ = sso_login(cfg["sso_username"], cfg["sso_password"])
    log(f"SSO 自动登录成功，新 token: {token[:12]}…")
    save_token(cfg_path, cfg, token)
    return token


def sso_cookie_refresh(cfg, cfg_path):
    """用保存的会话 cookie 免密换新 token（GET /login 直接 302 带 ticket）。

    成功返回新 token 并更新 config/sso_session；失败返回 None。
    """
    from urllib.parse import urljoin
    sess_file = Path(cfg_path).parent / "sso_session.json"
    if not sess_file.exists():
        return None
    cookies = json.loads(sess_file.read_text(encoding="utf-8"))
    s = requests.Session()
    for ck in cookies:
        s.cookies.set(ck["name"], ck["value"], domain=ck.get("domain"), path=ck.get("path", "/"))
    from sso_login import DESKTOP_UA
    s.headers.update({"User-Agent": DESKTOP_UA})

    svc = cfg.get("base_url", BASE_URL).rstrip("/") + "/oauth2/callback.action"
    r = s.get("https://sso.fzu.edu.cn/login", params={"service": svc},
              allow_redirects=False, timeout=20)
    if r.status_code != 302 or "ticket=" not in (r.headers.get("Location") or ""):
        log("会话 cookie 已失效（未直接拿到 ticket）")
        return None

    token, url, cur, hops = None, r.headers.get("Location"), "https://sso.fzu.edu.cn/login", 0
    while url and hops < 10:
        hops += 1
        full = url if url.startswith("http") else urljoin(cur, url)
        m = re.search(r"[?&]token=([A-Za-z0-9_\-]+)", full)
        if m:
            token = m.group(1)
            break
        r = s.get(full, allow_redirects=False, timeout=20)
        cur = full
        url = r.headers.get("Location")
    if not token:
        return None

    # 验证 token 真实可用
    r = requests.post(cfg.get("base_url", BASE_URL).rstrip("/") + "/init.action",
                      json={}, headers={"token": token}, timeout=20)
    if r.status_code != 200:
        return None
    save_token(cfg_path, cfg, token)
    sess_file.write_text(json.dumps(
        [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in s.cookies],
        ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"会话 cookie 换新 token 成功: {token[:12]}…")
    return token


def main():
    ap = argparse.ArgumentParser(description="福大晚点名签到")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--token", help="直接传 token，覆盖 config")
    ap.add_argument("--dry-run", action="store_true", help="只走到提交前一步，不调用 clockIn")
    ap.add_argument("--once", action="store_true", help="只判定一轮，不轮询等待")
    args = ap.parse_args()

    cfg_path = args.config
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    token = args.token or cfg.get("token")
    if not token and not (cfg.get("sso_username") and cfg.get("sso_password")):
        sys.exit("没有 token 且未配置 SSO 凭据（sso_username/sso_password）")
    if not token:
        token = sso_cookie_refresh(cfg, cfg_path) or sso_refresh(cfg, cfg_path)

    client = AttnClient(token, cfg.get("base_url", BASE_URL), user_agent=cfg.get("user_agent"))

    # 轮询上限：超过 max_wait_minutes 仍不满足条件就退出（防止清单不下发时空转）
    deadline = time.time() + int(cfg.get("max_wait_minutes", 150)) * 60
    sso_relogins = 0
    err_streak, last_err, ai_marked = [0], [""], [False]

    while True:
        try:
            state = run_once(client, cfg, dry_run=args.dry_run)
        except requests.HTTPError as e:
            log(f"HTTP 错误: {e}")
            state = STATE_ABORT
        except RuntimeError as e:
            if "token 可能已失效" in str(e) and sso_relogins < 3:
                sso_relogins += 1
                log(f"token 失效，第 {sso_relogins}/3 次自动续期…")
                try:
                    nt = sso_cookie_refresh(cfg, cfg_path) or sso_refresh(cfg, cfg_path)
                    client = AttnClient(nt, cfg.get("base_url", BASE_URL),
                                        user_agent=cfg.get("user_agent"))
                    continue
                except Exception as e2:
                    log(f"自动续期失败: {e2}")
                    try:
                        from ai_assist import ai_help
                        advice = ai_help("token 自动续期失败，脚本即将退出", {"error": str(e2)[:500]})
                        log(f"[AI 建议] {advice}")
                    except Exception:
                        pass
                    sys.exit(2)
            log(f"异常: {e}")
            state = STATE_ABORT
        except requests.RequestException as e:
            log(f"网络异常: {e}")
            client.conn_err = True   # 连接被拒/重置：可能是 WAF，退避拉长避免加重封锁
            state = STATE_WAIT
            err_streak[0] += 1
            last_err[0] = f"{type(e).__name__}: {e}"
        except Exception as e:
            log(f"异常: {e}")
            state = STATE_WAIT
            err_streak[0] += 1
            last_err[0] = f"{type(e).__name__}: {e}"

        if state in (STATE_DONE, STATE_ABORT) or getattr(client, "in_window", False):
            err_streak[0] = 0
        if err_streak[0] >= 3 and not ai_marked[0]:
            ai_marked[0] = True
            try:
                from ai_assist import ai_help
                advice = ai_help("连续多轮异常", {"streak": err_streak[0], "last_error": last_err[0][:400]})
                log(f"[AI 建议] {advice}")
            except Exception:
                pass
        if args.once or state in (STATE_DONE, STATE_ABORT):
            sys.exit(0 if state == STATE_DONE else 1)

        if time.time() > deadline:
            log(f"等待超时（max_wait_minutes={cfg.get('max_wait_minutes', 150)}），退出")
            sys.exit(1)

        # STATE_WAIT：等时间窗/清单下发。已在窗口内（服务器忙/围栏未命中）→ 快速重试
        if getattr(client, "conn_err", False):
            interval = max(int(cfg.get("in_window_poll_interval", 60)), 180)
            client.conn_err = False
        elif getattr(client, "in_window", False):
            interval = int(cfg.get("in_window_poll_interval", 60))
        else:
            interval = int(cfg.get("poll_interval", 300))
        log(f"等待 {interval}s 后重试…\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
