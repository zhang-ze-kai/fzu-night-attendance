#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""值守 AI 助手：突发情况时把脱敏现场发给 GLM，取回诊断与白名单处置决策。"""
import json
import re

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = "glm-5.3-flash"

SYSTEM = """你是福大晚点名自动签到脚本的值守助手。脚本跑在阿里云服务器（cron 每天 21:10），
通过 yzsxg.fzu.edu.cn 的 H5 接口签到。已知协议要点：
- init.action(POST json {})/currentTimestamp.action(GET)/check.action/clockIn.action(GET param=双重URL编码的AES-CBC(key=IV=apexinfoapexinfo))
- 请求头 token: <H5 token>；token 失效特征=HTTP 500 + HTML 提示页或非 JSON
- token 续期链：带 sso_session.json 的 cookie GET sso.fzu.edu.cn/login?service=<callback> → 302 带 ticket → callback → authorize.action?token=<新token>（Location 可能是相对路径需 urljoin）；init 前必须带 token 头
- 会话失效特征：GET /login 不再 302（200 登录页）→ 需短信 2FA，只能人工重跑 bootstrap.py 由用户报码
- 时间窗 21:00-23:59（服务端分钟），迟到线 22:30，围栏=旗山校区 3 多边形，FWay=1
- clockIn 成功返回 {success:true,code:1,data:{attn或直连字段 checkInTime...}}；服务器高峰可能闪断/限流（连接重置），脚本有 8 次快速重试
任务：根据现场数据诊断问题，给维护者简洁可执行的处置建议。禁止索要或输出密码/token 全文。150 字内。"""

DECIDE_SYSTEM = """你是签到脚本的值守 AI。脚本遇到异常时，你可以从白名单动作里选一个让脚本执行。
可用动作：
- renew_session: 重新用会话 cookie 换 token（适用于 token 失效/401/非 JSON 响应；cookie 已失效时无效）
- keep_waiting: 维持当前重试节奏（适用于服务器抖动/高峰拥堵/连接重置等瞬时故障）
- escalate: 超出脚本能力（如需短信验证码、代码级 bug、围栏配置问题），通知人工处理
只返回 JSON（不要其他文字）: {"action": "renew_session|keep_waiting|escalate", "reason": "一句话理由"}"""

_calls = 0  # 每次脚本运行 ai_help 最多调用 2 次


def _load_key(api_key=None):
    if api_key:
        return api_key
    try:
        cfg = json.load(open("config.json", encoding="utf-8"))
        return cfg.get("glm_api_key")
    except Exception:
        return None


def _chat(messages, timeout=30, max_tokens=1024):
    import requests
    key = _load_key()
    if not key:
        raise RuntimeError("未配置 glm_api_key")
    r = requests.post(API_URL, timeout=timeout, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "max_tokens": max_tokens})
    j = r.json()
    if "choices" not in j:
        raise RuntimeError(f"AI 响应异常: {json.dumps(j, ensure_ascii=False)[:200]}")
    msg = j["choices"][0].get("message", {})
    out = (msg.get("content") or "").strip()
    if not out:
        out = (msg.get("reasoning_content") or "").strip()[-400:]
    return out or "（AI 返回为空）"


def ai_help(issue, context=None, timeout=30, api_key=None):
    """返回 AI 诊断文本；任何异常都不抛出（绝不影响签到主流程）。"""
    global _calls
    if _calls >= 2:
        return "（本次运行 AI 调用已达上限，跳过）"
    _calls += 1
    try:
        if not _load_key(api_key):
            return "（未配置 glm_api_key）"
        ctx = json.dumps(context or {}, ensure_ascii=False, default=str)[:1500]
        return _chat([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "现场情况：" + issue + " 上下文数据：" + ctx + " 请诊断并给处置建议。"},
        ], timeout)
    except Exception as e:
        return f"（AI 诊断失败: {type(e).__name__}: {e}）"


def ai_decide(issue, context=None, timeout=30):
    """异常场景下让 AI 从白名单动作里选一个。返回 (action, reason)。"""
    try:
        ctx = json.dumps(context or {}, ensure_ascii=False, default=str)[:1200]
        text = _chat([
            {"role": "system", "content": DECIDE_SYSTEM},
            {"role": "user", "content": "现场情况：" + issue + " 上下文：" + ctx},
        ], timeout)
        m = re.search(r"\{[^{}]*\}", text, re.S)
        if m:
            d = json.loads(m.group(0))
            act = d.get("action")
            if act in ("renew_session", "keep_waiting", "escalate"):
                return act, d.get("reason", "")
        return None, text[-300:]
    except Exception as e:
        return None, f"AI 决策失败: {type(e).__name__}: {e}"
