#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码医生：当运维级修复（换会话/重试）与白名单决策都无法解决时，
AI 接入做代码级诊断，并对外科手术式修补提供函数级替换（编译校验+备份+自动回滚）。

流程：
  1. 收集证据：失败动作的原始响应报文（client.last_response）+ 失败函数当前源码 + 环境
  2. GLM 返回 JSON {diagnosis, fix_type: none|patch, file, function_name, patched_function_source, reason}
  3. patch 校验链：AST 定位原函数 → 函数级替换 → 内存编译校验 → 备份原文件 → 写入
     → 子进程导入自检 → 失败自动从备份回滚
安全边界：
  - 只允许修补 ALLOW_FILES 白名单文件
  - 每个失败签名每天最多自动修补 1 次
  - 原文件保留 .bak-<时间戳>；修补即邮件通知用户审阅
  - 任何失败降级为 escalate 文本，绝不影响签到主流程
"""
import ast
import hashlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
from pathlib import Path

ALLOW_FILES = {"checkin.py", "sso_login.py", "notifier.py"}
STATE_FILE = Path(__file__).resolve().parent / "code_doctor_state.json"

DOCTOR_SYSTEM = """你是签到系统的代码医生。夜间自动签到脚本遇到运维手段（换会话/重试）无法解决的问题，
现在把失败接口的原始响应、相关函数的当前源码、运行环境交给你做代码级诊断。

只返回严格的 JSON（不要其他文字）：
{
  "diagnosis": "根因诊断（80字内）",
  "fix_type": "none 或 patch",
  "file": "若 patch：目标文件名（只允许 checkin.py / sso_login.py / notifier.py）",
  "function_name": "若 patch：要替换的函数/方法名",
  "patched_function_source": "若 patch：该函数修复后的完整 def 块（含原缩进级别）",
  "reason": "若 fix_type=none：代码层为何无法解决，人工需要做什么"
}
规则：
- 只有当证据足以确定代码级根因、且修复局限于单个函数时才用 patch；否则 fix_type=none
- patched_function_source 除修复点外必须与原函数行为/签名一致
- 不得输出任何真实密码/token（证据已脱敏）"""

STATE = {"calls": 0}


def _load_key(api_key=None):
    if api_key:
        return api_key
    here = Path(__file__).resolve().parent
    for cand in [Path("config.json"), here / "config.json"]:
        try:
            k = json.loads(cand.read_text(encoding="utf-8")).get("glm_api_key")
            if k:
                return k
        except Exception:
            continue
    acc = here / "accounts"
    if acc.exists():
        for d in sorted(acc.iterdir()):
            try:
                k = json.loads((d / "config.json").read_text(encoding="utf-8")).get("glm_api_key")
                if k:
                    return k
            except Exception:
                continue
    return None


def _chat(messages, timeout=300, max_tokens=16384):
    import requests
    key = _load_key()
    if not key:
        raise RuntimeError("未配置 glm_api_key")
    r = requests.post("https://open.bigmodel.cn/api/paas/v4/chat/completions",
                      timeout=timeout,
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                      json={"model": "glm-5.3-flash", "messages": messages, "max_tokens": max_tokens})
    j = r.json()
    if "choices" not in j:
        raise RuntimeError(f"AI 响应异常: {json.dumps(j, ensure_ascii=False)[:200]}")
    msg = j["choices"][0].get("message", {})
    out = (msg.get("content") or "").strip()
    if not out:
        out = (msg.get("reasoning_content") or "").strip()
    return out


def _extract_json(text):
    """解析 AI 返回的 JSON；对截断输出做渐进修复，字段级兜底提取。"""
    import re
    m = re.search(r"\{.*\}", text, re.S)
    candidates = []
    if m:
        candidates.append(m.group(0))
    # 截断修复：从最长到最短逐个 '}' 尝试
    if m:
        blob = m.group(0)
        ends = [i for i, ch in enumerate(blob) if ch == "}"]
        for i in reversed(ends):
            candidates.append(blob[:i + 1])
    for c in candidates:
        try:
            d = json.loads(c)
            if isinstance(d, dict) and "diagnosis" in d:
                return d
        except Exception:
            continue
    # 字段级兜底：JSON 彻底截断时逐字段正则提取
    d = {}
    md = re.search(r'"diagnosis"\s*:\s*"([^"]*)"', text)
    if md: d["diagnosis"] = md.group(1)
    mf = re.search(r'"fix_type"\s*:\s*"(\w+)"', text)
    if mf: d["fix_type"] = mf.group(1)
    mfn = re.search(r'"function_name"\s*:\s*"([^"]+)"', text)
    if mfn: d["function_name"] = mfn.group(1)
    mf2 = re.search(r'"file"\s*:\s*"(\w+\.py)"', text)
    if mf2: d["file"] = mf2.group(1)
    # 补丁源码：定位标记之间的内容
    mp = re.search(r'"patched_function_source"\s*:\s*"(.*?)(?:",\s*"\w+"|",\s*"reason"|$)', text, re.S)
    if mp:
        ps = mp.group(1)
        NL = chr(10)
        TB = chr(9)
        QT = chr(34)
        ps = ps.replace("\\n", NL).replace("\\t", TB).replace('\\"', QT)
        d["patched_function_source"] = ps
    return d if d.get("diagnosis") or d.get("fix_type") else None


def _signature(issue):
    return hashlib.md5((issue + time.strftime("%Y%m%d")).encode()).hexdigest()


def _already_patched_today(sig):
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return st.get("sig") == sig and st.get("date") == time.strftime("%Y%m%d")
    except Exception:
        return False


def _mark_patched(sig):
    STATE_FILE.write_text(json.dumps({"sig": sig, "date": time.strftime("%Y%m%d")}),
                           encoding="utf-8")


def _splice_function(orig_src, fname, new_fn_src):
    """AST 定位函数（含类方法/装饰器），函数级替换。返回 (new_src, start, end)。"""
    tree = ast.parse(orig_src)
    lines = orig_src.splitlines(keepends=True)
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
            if best is None or node.end_lineno > best.end_lineno:
                best = node
    if best is None:
        raise ValueError(f"函数 {fname} 不存在")
    start = (best.decorator_list[0].lineno - 1) if best.decorator_list else best.lineno - 1
    end = best.end_lineno
    fn = new_fn_src if new_fn_src.endswith("\n") else new_fn_src + "\n"
    return "".join(lines[:start]) + fn + "".join(lines[end:]), start, end


def diagnose_and_maybe_patch(issue, evidence, failing_action=None, api_key=None,
                             app_dir=None, dry_run=False):
    """AI 代码医生。返回 {diagnosis, patched, rolled_back, function_name, notify_text}。"""
    here = Path(__file__).resolve().parent
    app_dir = Path(app_dir) if app_dir else here
    result = {"diagnosis": "", "patched": False, "rolled_back": False,
              "function_name": "", "notify_text": ""}

    sig = _signature(issue + json.dumps(evidence, ensure_ascii=False, default=str)[:500])
    if _already_patched_today(sig) and not dry_run:
        result["notify_text"] = "（该问题今天的自动修补已尝试过，不再重复）"
        return result

    # 1. 证据包（脱敏）
    import re as _re
    ev = json.dumps(evidence, ensure_ascii=False, default=str)
    for pat in [r'"sso_password":\s*"[^"]*"', r'"token":\s*"[^"]{8,}"',
                r'"glm_api_key":\s*"[^"]*"', r'"notify_key":\s*"[^"]*"']:
        ev = _re.sub(pat, '"<已脱敏>"', ev)
    ev = ev[:3500]

    # 2. 失败接口相关源码片段
    src_code = ""
    target_file = (evidence.get("file") if evidence.get("file") in ALLOW_FILES else "checkin.py")
    try:
        mod_src = (app_dir / target_file).read_text(encoding="utf-8")
        if failing_action:
            i = mod_src.find('"' + failing_action + '"')
            src_code = ("--- " + target_file + " 与 " + failing_action
                        + " 相关片段 ---\n" + mod_src[max(0, i - 3000): i + 1500])
        else:
            src_code = mod_src[:4500]
    except Exception as e:
        src_code = f"（源码读取失败: {e}）"

    # 3. GLM 代码级诊断
    try:
        text = _chat([
            {"role": "system", "content": DOCTOR_SYSTEM},
            {"role": "user", "content": (f"问题：{issue}\n失败接口：{failing_action}\n"
                                          f"原始响应/证据（已脱敏）：\n{ev}\n\n相关源码：\n{src_code[:6000]}")},
        ])
    except Exception as e:
        result["diagnosis"] = f"AI 调用失败: {e}"
        result["notify_text"] = "AI 代码医生调用失败，请人工查看 run.log。"
        return result

    d = _extract_json(text)
    if not d or "diagnosis" not in d:
        result["diagnosis"] = text[-600:]
        result["notify_text"] = "AI 诊断未返回结构化结果，请人工查看。"
        return result

    result["diagnosis"] = d.get("diagnosis", "")
    fix_type = d.get("fix_type", "none")
    target = d.get("file", "")
    fname = d.get("function_name", "")
    fn_src = d.get("patched_function_source", "")

    # 4. 函数级补丁：AST 替换 → 内存编译校验 → 备份 → 写入 → 子进程导入自检 → 失败回滚
    if fix_type == "patch" and target in ALLOW_FILES and fname and fn_src:
        fpath = app_dir / target
        try:
            orig = fpath.read_text(encoding="utf-8")
            new_src, _, _ = _splice_function(orig, fname, fn_src)
            compile(new_src, target, "exec")  # 内存编译校验
        except Exception as pe:
            result["notify_text"] = f"AI 补丁校验失败已丢弃: {pe}。诊断: {result['diagnosis']}"
            return result

        if dry_run:
            result["patched"] = True
            result["function_name"] = fname
            result["notify_text"] = f"dry_run 测试：函数 {fname} 补丁编译校验通过（未写入生产）。诊断: {result['diagnosis']}"
            return result

        bak = app_dir / (target + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
        try:
            shutil.copy(fpath, bak)
            fpath.write_text(new_src, encoding="utf-8")
            py_compile.compile(str(fpath), doraise=True)
            chk = subprocess.run([sys.executable, "-c",
                                  f"import sys; sys.path.insert(0, r'{app_dir}'); import {target[:-3]}"],
                                 capture_output=True, timeout=30)
            if chk.returncode != 0:
                raise RuntimeError("导入自检失败: " + chk.stderr.decode("utf-8", "replace")[-200:])
            result["patched"] = True
            result["function_name"] = fname
            result["notify_text"] = (f"AI 已自动修补 {target} 的函数 {fname}"
                                     f"（原文件备份 {bak.name}）。诊断: {result['diagnosis']}")
            _mark_patched(sig)
        except Exception as pe:
            if bak.exists():
                shutil.copy(bak, fpath)
                result["rolled_back"] = True
            result["notify_text"] = (f"AI 补丁应用失败已自动回滚（从 {bak.name} 恢复）。"
                                     f"错误: {pe}。诊断: {result['diagnosis']}")
    else:
        result["notify_text"] = ("AI 判定代码层无法自动修复，需人工处理。诊断: " + result["diagnosis"]
                                 + " 人工指引: " + str(d.get("reason", ""))[:300])
    return result
