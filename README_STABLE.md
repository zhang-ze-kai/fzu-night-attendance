# 福大晚点名自动签到 — 稳定分发版

给同学的独立运行包：**填好自己的配置，跑一次引导，之后每天自动签到**。

> **有 AI 助手？** 把 [AI_SETUP.md](AI_SETUP.md) 和整个目录一起发给你的 AI 助手
> （ZCode / ChatGPT / Claude 均可），说一句"帮我按这个文档配置"，
> 它会引导你完成全部步骤（含收集信息、写配置、引导短信、挂定时）。
> 本文档剩余部分供手动配置者阅读。
架构与生产版一致（含可选的 AI 自动处置 + 邮件通知），全部槽位自己填自己的。

## 快速开始（约 10 分钟）

### 0. 要求
- Python 3.10+（Windows/Linux 均可）
- 一台能常开的机器（自己的电脑 / 服务器 / Termux 均可）

### 1. 安装依赖
```bash
pip install requests pycryptodome
# 首次短信引导需要（仅一次）：
pip install playwright && playwright install chromium
```

### 2. 建账号目录
把 `accounts/你的学号/` 目录复制一份，目录名改成**你的学号**，
将里面的 `config.example.json` 重命名为 `config.json` 并填写：

| 字段 | 必填 | 说明 |
|---|---|---|
| sso_username / sso_password | ✅ | 学号 + 信息门户密码（用于自动登录，只存本机） |
| lon / lat | ✅ | 你宿舍坐标，从 https://lbs.amap.com/tools/picker 拾取（格式 `经度,纬度`） |
| address | ✅ | 展示用地址文字 |
| glm_api_key | 可选 | 智谱开放平台 key，开启 AI 自动处置 |
| notify_channel / notify_key | 可选 | 邮件通知（格式见 config 内注释），签到结果发邮箱 |
| token | 留空 | 会自动登录获取 |

### 3. 一次性短信引导（新环境首次登录必须）
```bash
python bootstrap.py 你的学号
```
脚本自动填账密登录 → 页面要求短信验证时自动点"获取验证码" →
**你手机收到验证码后**，把验证码写进账号目录的 `sms_code.txt`（一行纯数字）：
```bash
echo -n 123456 > accounts/你的学号/sms_code.txt    # Linux/Mac
echo 123456> accounts\你的学号\sms_code.txt        # Windows
```
脚本自动完成登录并保存会话。**之后长期免密自动运行。**

### 4. 手动验证
```bash
python main.py --dry-run --once        # 白天跑会显示"不在签到时间范围"= 正常
```

### 5. 定时任务
**Windows**（管理员 CMD）：
```bat
schtasks /create /tn "FDU-checkin" /tr "python C:\路径\main.py" /sc daily /st 21:10 /f
```
**Linux**（crontab -e，若时区非北京时间先 `date` 确认）：
```
10 21 * * * cd /路径 && python3 main.py >> run.log 2>&1
```

## 每晚自动发生什么

21:10 启动 → 用会话 cookie 免密换 token → 服务端校时确认在 21:00-23:59 窗口内
→ 坐标加 ±30 米抖动 → 服务端验证围栏命中 → 模拟真人节奏（2 次定位轮询 + 30-180 秒随机延迟）
→ 提交打卡（失败自动重试 8 次）→ 成功即退出。
异常时 AI 自动决策（换会话/继续等待/通知你），配了邮箱的话结果发你邮箱。

## 常见问题

| 现象 | 处理 |
|---|---|
| 日志报「token 可能已失效」 | 脚本会自动续期；若续期也失败并提示「短信二次认证」→ 重跑第 3 步 bootstrap |
| 「定位未命中围栏」 | 坐标不对——重新拾取宿舍坐标 |
| 手机上跑 | 可用 Termux（Python 环境），但更推荐电脑或服务器 |
| 想换坐标/换邮箱 | 改 config.json 即可，下次运行生效 |

## 隐私与风险提示

- 密码/授权码只存在本机 config.json，不上传任何第三方（AI 诊断调用只发送脱敏的错误摘要）
- 本脚本模拟你本人操作，坐标请填真实宿舍位置；使用责任自负
- 账号安全：如遇学校风控提示，立即停止使用
