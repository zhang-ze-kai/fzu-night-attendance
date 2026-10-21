# 福州大学晚点名自动化插件

> 智汇福大 App「晚点名签到」H5 服务的自动化签到工具。
> 支持多人、AI 辅助故障自愈、邮件通知。**仅供学习研究，请遵守学校规定，使用风险自负。**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20Termux-lightgrey)]()

## 功能特性

- 🌙 **每晚自动签到**：定时触发，服务端校时确认签到窗口（21:00-23:59），无需人工干预
- 📍 **围栏判定**：向服务端提交坐标，由服务端多边形围栏算法判定是否命中可签到校区
- 🤖 **AI 值守（可选）**：异常时调用大模型从白名单动作中自主决策（续期会话/继续重试/上报人工），上下文多轮滚动
- 📧 **邮件通知（可选）**：签到成功/失败/需人工处理，三级邮件主动推送
- 👥 **多账号**：`accounts/` 目录按学号隔离，遍历执行，账号间随机间隔防行为聚集
- 🛡️ **拟人化设计**：坐标 ±30m 抖动、30-180s 随机提交延迟、定位轮询节奏模拟、低请求量
- 🔄 **三级凭证续期**：H5 token → 会话 cookie 免密换票 → 账密登录（新环境首次触发短信 2FA，引导一次即长期免密）
- 📄 **AI 可配置**：附 `AI_SETUP.md` 操作手册，把目录丢给任意 AI 助手即可引导完成全部配置

## 工作原理

```
登录链（一次性引导）:
  账密 POST sso.fzu.edu.cn(CAS) → 短信 2FA → 会话 cookie（长期复用）

每晚 21:10:
  cookie 免密 GET /login?service=  →  302 ticket
  → oauth2/callback  →  authorize.action?token=   （H5 会话凭证）
  → init.action      （考勤计划/时间窗/围栏/个人状态）
  → currentTimestamp.action（服务端校时，防改本机时间）
  → check.action     （提交坐标，服务端多边形围栏判定）
  → clockIn.action   （提交打卡，返回 checkInTime）
```

- 通信加密：业务参数 AES-128-CBC（key=IV 硬编码于页面 JS），GET 参数**双重 URL 编码**
  （页面 encodeURIComponent + axios params 序列化两层，服务端解两次——单层编码会因
  base64 的 `+` 被二次解码为空格导致密文损坏）
- 全部请求携带 `token` 请求头；token 失效自动走续期链

## 目录结构

```
├── main.py              主入口：遍历 accounts/ 逐账号签到（账号间 3-8 分钟随机间隔）
├── checkin.py           核心客户端：加密/四接口/状态机（9 态）/重试
├── sso_login.py         CAS 账密登录 + 会话 cookie 免密换票
├── ai_assist.py         GLM 值守 AI：诊断 + 白名单动作决策
├── notifier.py          多渠道通知：邮件 / Server酱 / PushPlus / 企业微信
├── bootstrap.py         无头浏览器短信 2FA 引导（一次性）
├── AI_SETUP.md          AI 助手配置手册（把目录丢给 AI 助手即可自动配置）
├── README_STABLE.md     人工配置指南
└── accounts/<学号>/     每账号独立：config.json + sso_session.json + 日志
```

## 快速开始

把整个目录丢给你的 AI 助手（ZCode / ChatGPT / Claude），说：

> “帮我按 AI_SETUP.md 配置福大晚点名自动签到”

或手动配置，见 [README_STABLE.md](README_STABLE.md)。核心步骤：

```bash
pip install requests pycryptodome
pip install playwright && playwright install chromium   # 仅首次短信引导需要

# 1. 复制 accounts/你的学号/config.example.json 为 accounts/<你的学号>/config.json 并填写
# 2. 首次引导（手机收到验证码后写入 accounts/<学号>/sms_code.txt）
python bootstrap.py <你的学号>
# 3. 验证（白天显示"不在签到时间范围"= 正常）
python main.py --dry-run --once
# 4. 挂定时任务（Windows schtasks / Linux crontab，见 README_STABLE.md）
```

## 配置说明

全部配置在 `accounts/<学号>/config.json`（模板见 `accounts/你的学号/config.example.json`）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `sso_username` / `sso_password` | ✅ | 学号 + 信息门户密码（仅存本机） |
| `lon` / `lat` | ✅ | 宿舍坐标（高德坐标系），须在可签到校区围栏内 |
| `address` | ✅ | 展示用地址文字 |
| `glm_api_key` | 可选 | 智谱开放平台 key，开启 AI 值守 |
| `notify_channel` / `notify_key` | 可选 | 邮件（`smtp.qq.com\|465\|邮箱\|授权码\|收件箱`）/ Server酱 / PushPlus / 企微 |
| `token` | 留空 | 自动获取并回写 |

## 隐私与安全设计

- 所有凭据**只存本机** `accounts/<学号>/config.json`，仓库不含任何真实凭据
- AI 诊断调用仅发送**脱敏的错误摘要**，密码/token 全文不会出现在任何外发请求中
- 每账号每晚请求量 ~15 个（对比真实页面单账号每秒 1 个的轮询），远低于正常使用强度

## ⚠️ 免责声明

本项目仅供学习研究 HTTP 协议分析与自动化技术之用。使用本工具产生的一切后果
（包括但不限于学校纪律处分）由使用者自行承担。请自觉遵守学校考勤管理规定，
按时回归宿舍。**签到记录造假在紧急情况（如夜间查寝、突发事件）下可能影响
学校对你的定位与救助，请勿在不具备实际在寝条件时使用。**
