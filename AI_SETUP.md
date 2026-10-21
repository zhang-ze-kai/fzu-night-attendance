# AI 配置手册 — 福大晚点名自动签到

> **本文档写给 AI 助手（你）**：用户会把本文件连同本目录交给你的 AI 助手（ZCode/ChatGPT/Claude 等），
> 要求你（AI）帮 TA 完成配置。请严格按本文档执行，不遗漏验证步骤，不越过第 10 节红线。
> 遇到本文档未覆盖的报错，先看第 9 节故障矩阵；仍无法解决时如实告知用户需要人工介入。

## 1. 任务概述

你要帮用户在 TA 的电脑/服务器上部署一个每晚 21:10 自动签到的脚本：
1. 安装 Python 依赖
2. 帮用户生成 `accounts/<学号>/config.json`（用户需提供：学号、密码、宿舍坐标）
3. 运行一次性短信引导（需要用户报手机验证码）
4. 验证 + 注册每日定时任务
5. （可选）配置 AI 诊断 key 和邮件通知

**工作目录**：本文件所在的目录（包含 main.py / checkin.py / bootstrap.py 等）。
以下所有命令默认在该目录下执行。

## 2. 前置检查

```bash
python --version    # 或 python3 --version，需 ≥ 3.10
```
- Windows 无 `python` 命令 → 引导安装：https://www.python.org/downloads/ （勾选 Add to PATH）
- 确认目录内有：`main.py` `checkin.py` `sso_login.py` `ai_assist.py` `notifier.py` `bootstrap.py` `README_STABLE.md`
  缺文件 → 让用户重新解压分发包。

## 3. 步骤一：安装依赖

```bash
pip install requests pycryptodome
pip install playwright && playwright install chromium
```
- Windows 若提示 `pip` 不是命令 → 用 `python -m pip install ...`
- Linux 报 `externally-managed-environment` → 建 venv：
  `python3 -m venv venv && venv/bin/pip install requests pycryptodome playwright && venv/bin/playwright install chromium`
  （之后所有 `python` 命令换 `venv/bin/python3`）
- playwright 的 chromium 下载慢/失败 → 重试即可，或设置镜像：
  `set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright`（Windows CMD）

## 4. 步骤二：创建账号配置

向用户收集：**学号**、**信息门户密码**（与智汇福大 App 登录密码相同）、**宿舍经纬度**。

坐标获取：让用户打开 https://lbs.amap.com/tools/picker 搜索/点选自己宿舍楼，
复制"经度,纬度"（形如 `119.195397,26.060246`，**经度在前**）。

生成配置（用学号替换 `<学号>`，值替换后写入）：

```bash
mkdir -p accounts/<学号>
```

写入 `accounts/<学号>/config.json`：

```json
{
  "token": "",
  "base_url": "https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/",
  "sso_username": "<学号>",
  "sso_password": "<信息门户密码>",
  "lon": <经度数值>,
  "lat": <纬度数值>,
  "address": "<地址文字，如：福建省福州市闽侯县福州大学旗山校区XX楼>",
  "ca_is_no": "0",
  "poll_interval": 300,
  "jitter_meters": 30,
  "warmup_checks": 2,
  "clockin_delay_range": [30, 180],
  "max_wait_minutes": 120,
  "submit_retries": 8,
  "in_window_poll_interval": 60,
  "glm_api_key": "",
  "notify_channel": "",
  "notify_key": ""
}
```

**安全要求**：写入前提醒用户——密码只存本机此文件，不要发到聊天记录/截图里给任何人。
坐标必须在用户可签到校区围栏内（校点错校区会一直"未命中围栏"）。

## 5. 步骤三：一次性短信引导（需要用户配合报验证码）

```bash
python bootstrap.py <学号>
```

流程（脚本自动执行）：填账密 → 点登录 → 服务端要求短信验证 → 自动点"获取验证码"
→ **此时用户手机会收到验证码短信** → 你把验证码写入文件：

```bash
# Linux/Mac
echo -n <验证码> > accounts/<学号>/sms_code.txt
# Windows CMD
echo <验证码>> accounts\<学号>\sms_code.txt
```

脚本每 2 秒轮询该文件（等 5 分钟），填入后自动完成登录，账号目录生成
`sso_session.json`（会话 cookie，长期免密）+ config 里的 token 被更新。

- 脚本报「短信二次认证」以外错误 → 见第 9 节
- 5 分钟超时 → 直接重跑 bootstrap.py 会重发短信

## 6. 步骤四：验证

```bash
python main.py --account <学号> --dry-run --once
```

**判定标准**：
- 输出 `今日已签到: status=SUCCESS` + `汇总: 成功 1` + exit 0 → 完全成功
- 输出 `不在签到时间范围 [21:00-23:59]` → ✅ 也算成功（白天正常现象，核心链路已通）
- 输出 `check.action 异常` / `打卡地点校验发生异常` → ❌ 配置的是旧版文件，重新分发包
- 输出 `定位未命中围栏` → 坐标问题，回到步骤二核对

## 7. 步骤五：定时任务

**Windows（需要管理员或用户级计划任务均可）**：
```bat
schtasks /create /tn "FDU-checkin" /tr "python C:\完整路径\main.py" /sc daily /st 21:10 /f
schtasks /query /tn "FDU-checkin"    ← 注册后必须读回验证
```
路径含空格时 `/tr "\"python\" \"C:\完整路径\main.py\""`。
提醒用户：电池/安全软件不要杀这个任务的宿主。

**Linux（crontab -e）**：
先 `date` 确认时区——若非 CST(UTC+8)，把小时换算成北京时间 21:10 对应的本地小时。
```
10 21 * * * cd /绝对路径 && python3 main.py >> run.log 2>&1
```
**注册后必须 `crontab -l` 读回验证**（曾出现过注册行丢失的事故）。

## 8. 可选配置

### 8.1 AI 自动处置（glm_api_key）
用户有智谱开放平台 key（https://open.bigmodel.cn 注册）→ 填入 config 的 `glm_api_key`。
效果：异常时 AI 自动决策（换会话/继续等待/上报），诊断写日志。不填不影响签到。

### 8.2 邮件通知（notify_channel/notify_key）
让用户提供**自己邮箱的 SMTP 授权码**（不是邮箱密码）：
- QQ 邮箱：网页版 → 设置 → 账号 → 开启 IMAP/SMTP → 获得 16 位授权码
- 163 邮箱：设置 → POP3/SMTP/IMAP → 开启 → 获得授权码

写入 config：
```json
{
  "notify_channel": "email",
  "notify_key": "smtp.qq.com|465|用户邮箱@qq.com|16位授权码|用户邮箱@qq.com"
}
```
效果：每晚签到后收邮件（全部成功/失败清单+AI 诊断）。支持其他渠道：
`notify_channel: "serverchan"`（key=SendKey，sct.ftqq.com 微信扫码获取）或 `"pushplus"`（key=token）。

## 9. 故障排查矩阵（症状 → 原因 → 修复）

| 日志/现象 | 原因 | 处置 |
|---|---|---|
| `不在签到时间范围` | 白天正常现象 | 无需处理 |
| `无晚点名清单` | 清单未下发 | 脚本会自动等到 23:45；无需处理 |
| `token 可能已失效` 后自动续期成功 | 正常 | 无需处理 |
| `需要短信二次认证` | 会话 cookie 过期 | 重跑 bootstrap.py，让用户再报一次码 |
| `打卡地点校验发生异常` | 使用了旧版文件（双重编码 bug） | 确认 checkin.py 来自本包 v1.0+ |
| `定位未命中围栏` 连续出现 | 坐标错误或当晚围栏更新 | 核对坐标；次日仍失败则检查校区是否匹配 |
| `连接被重置`/`Connection reset` | 学校 WAF 限流本 IP | 脚本自动退避；多次出现建议换网络环境 |
| `AI 响应异常 1210` | glm key 或参数问题 | 检查 glm_api_key；确认 main.py 为本包版本 |
| `缺少 playwright` | bootstrap 依赖未装 | 按第 3 节命令安装 |
| cron 到点没运行 | 注册丢失/机器关机 | `crontab -l` 或 `schtasks /query` 核对后重注册 |
| 邮件显示「签到失败」 | AI 会自动修复并重试 | 等 22:00 前的成功邮件；收到「请手动处理」邮件则人工打开 App 签到（22:30 前不算迟到） |
| 邮件显示「全部成功」 | 当晚签到完成 | 无需处理 |

## 10. 红线（AI 助手必须遵守）

1. **禁止在 21:00-23:59 窗口外提交真实打卡**（dry-run 验证随时可以）
2. **禁止对 yzsxg.fzu.edu.cn / sso.fzu.edu.cn 做高频或批量探测**（会触发 WAF 封 IP，
   曾发生过：连续测试导致用户整个网络被限）
3. 用户密码/授权码只写入本机 config.json，**不得出现在发给任何 API 的内容里**
4. 修改任何文件前先备份原文件；修改 crontab 后必须读回验证
5. 如实报告结果：失败就是失败，不虚构成功；无法解决时明确告知用户
6. 签到窗口内（21:00-23:59）出现失败：优先引导用户在本会话内修复重跑，窗口关闭前都有机会

## 11. 成功判据（最终验收）

以下任一即为配置成功：
- 窗口内运行后，`accounts/<学号>/config.json` 同级日志出现 `打卡返回` 且含 `checkInTime`
- 用户手机智汇福大晚点名页面显示绿色「签到成功」
- 邮件通知收到「晚点名签到： 全部成功」
